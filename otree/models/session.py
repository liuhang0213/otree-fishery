#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import channels
import json
import django.test
from django.core.urlresolvers import reverse

from otree import constants_internal
import otree.common_internal
from otree.common_internal import (
    random_chars_8, random_chars_10, get_admin_secret_code,
    get_app_label_from_name
)
from otree.db import models
from otree.models_concrete import ParticipantToPlayerLookup, RoomToSession
from django.template.loader import get_template
from django.template import TemplateDoesNotExist
from .varsmixin import ModelWithVars
logger = logging.getLogger('otree')

client = django.test.Client()

ADMIN_SECRET_CODE = get_admin_secret_code()

class Session(ModelWithVars):

    class Meta:
        app_label = "otree"
        # if i don't set this, it could be in an unpredictable order
        ordering = ['pk']

    _pickle_fields = ['vars', 'config']
    config = models._PickleField(default=dict, null=True)  # type: dict

    # label of this session instance
    label = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='For internal record-keeping')

    experimenter_name = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='For internal record-keeping')

    ready = models.BooleanField(default=False)

    code = models.CharField(
        default=random_chars_8,
        max_length=16,
        # set non-nullable, until we make our CharField non-nullable
        null=False,
        unique=True,
        doc="Randomly generated unique identifier for the session.")

    mturk_HITId = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Hit id for this session on MTurk')
    mturk_HITGroupId = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Hit id for this session on MTurk')

    # since workers can drop out number of participants on server should be
    # greater than number of participants on mturk
    # value -1 indicates that this session it not intended to run on mturk
    mturk_num_participants = models.IntegerField(
        default=-1,
        help_text="Number of participants on MTurk")

    mturk_use_sandbox = models.BooleanField(
        default=True,
        help_text="Should this session be created in mturk sandbox?")

    archived = models.BooleanField(
        default=False,
        db_index=True,
        doc=("If set to True the session won't be visible on the "
             "main ViewList for sessions"))

    comment = models.TextField(blank=True)

    _anonymous_code = models.CharField(
        default=random_chars_10, max_length=10, null=False, db_index=True)

    _pre_create_id = models.CharField(max_length=255, db_index=True, null=True)

    use_browser_bots = models.BooleanField(default=False)

    # if the user clicks 'start bots' twice, this will prevent the bots
    # from being run twice.
    _cannot_restart_bots = models.BooleanField(default=False)
    _bots_finished = models.BooleanField(default=False)
    _bots_errored = models.BooleanField(default=False)
    _bot_case_number = models.PositiveIntegerField()

    is_demo = models.BooleanField(default=False)

    # whether SOME players are bots
    has_bots = models.BooleanField(default=False)

    _admin_report_app_names = models.TextField(default='')
    _admin_report_num_rounds = models.CharField(default='', max_length=255)

    num_participants = models.PositiveIntegerField()

    def __unicode__(self):
        return self.code

    @property
    def participation_fee(self):
        '''This method is deprecated from public API,
        but still useful internally (like data export)'''
        return self.config['participation_fee']

    @property
    def real_world_currency_per_point(self):
        '''This method is deprecated from public API,
        but still useful internally (like data export)'''
        return self.config['real_world_currency_per_point']

    def is_for_mturk(self):
        return (not self.is_demo) and (self.mturk_num_participants > 0)

    def get_subsessions(self):
        lst = []
        app_sequence = self.config['app_sequence']
        for app in app_sequence:
            models_module = otree.common_internal.get_models_module(app)
            subsessions = models_module.Subsession.objects.filter(
                session=self
            ).order_by('round_number')
            lst.extend(list(subsessions))
        return lst

    def get_participants(self):
        return self.participant_set.all()

    def _create_groups_and_initialize(self):
        # group_by_arrival_time_time code used to be here
        for subsession in self.get_subsessions():
            subsession._create_groups()
            subsession.before_session_starts()
            subsession.creating_session()
            subsession.save()

    def mturk_requester_url(self):
        if self.mturk_use_sandbox:
            requester_url = (
                "https://requestersandbox.mturk.com/mturk/manageHITs"
            )
        else:
            requester_url = "https://requester.mturk.com/mturk/manageHITs"
        return requester_url

    def mturk_worker_url(self):
        if self.mturk_use_sandbox:
            return (
                "https://workersandbox.mturk.com/mturk/preview?groupId={}"
            ).format(self.mturk_HITGroupId)
        return (
            "https://www.mturk.com/mturk/preview?groupId={}"
        ).format(self.mturk_HITGroupId)

    def advance_last_place_participants(self):

        participants = self.get_participants()

        # in case some participants haven't started
        unvisited_participants = []
        for p in participants:
            if p._index_in_pages == 0:
                unvisited_participants.append(p)
                client.get(p._start_url(), follow=True)

        if unvisited_participants:
            # that's it -- just visit the start URL, advancing by 1
            return

        last_place_page_index = min([p._index_in_pages for p in participants])
        last_place_participants = [
            p for p in participants
            if p._index_in_pages == last_place_page_index
        ]

        for p in last_place_participants:
            try:
                current_form_page_url = p._current_form_page_url
                if current_form_page_url:
                    resp = client.post(
                        current_form_page_url,
                        data={
                            constants_internal.timeout_happened: True,
                            constants_internal.admin_secret_code: ADMIN_SECRET_CODE
                        },
                        follow=True
                    )
                    # not sure why, but many users are getting HttpResponseNotFound
                    if resp.status_code >= 400:
                        msg = ('Submitting page {} failed, '
                            'returned HTTP status code {}.'.format(
                                current_form_page_url, resp.status_code
                            ))
                        content = resp.content
                        if len(content) < 600:
                            msg += ' response content: {}'.format(content)
                        raise AssertionError(msg)

                else:
                    # it's possible that the slowest user is on a wait page,
                    # especially if their browser is closed.
                    # because they were waiting for another user who then
                    # advanced past the wait page, but they were never
                    # advanced themselves.
                    start_url = p._start_url()
                    resp = client.get(start_url, follow=True)
            except:
                logging.exception("Failed to advance participants.")
                raise


            # do the auto-advancing here,
            # rather than in increment_index_in_pages,
            # because it's only needed here.
            channels.Group(
                'auto-advance-{}'.format(p.code)
            ).send(
                {'text': json.dumps(
                    {'auto_advanced': True})}
            )

    def pages_auto_reload_when_advanced(self):
        # keep it enable until I determine
        # (a) the usefulness of the feature
        # (b) the impact on performance
        return True
        # return settings.DEBUG or self.is_demo

    def build_participant_to_player_lookups(self):
        subsession_app_names = self.config['app_sequence']

        views_modules = {}
        for app_name in subsession_app_names:
            views_modules[app_name] = (
                otree.common_internal.get_views_module(app_name))

        def views_module_for_player(player):
            return views_modules[player._meta.app_config.name]

        records_to_create = []

        for participant in self.get_participants():
            page_index = 0
            for player in participant.get_players():
                for View in views_module_for_player(player).page_sequence:
                    page_index += 1
                    records_to_create.append(
                        ParticipantToPlayerLookup(
                            participant=participant,
                            page_index=page_index,
                            app_name=player._meta.app_config.name,
                            player_pk=player.pk,
                            url=reverse(View.url_name(),
                                        args=[participant.code, page_index]))
                    )

            # technically could be stored at the session level
            participant._max_page_index = page_index
            participant.save()
        ParticipantToPlayerLookup.objects.bulk_create(records_to_create)

    def get_room(self):
        from otree.room import ROOM_DICT
        try:
            room_name = RoomToSession.objects.get(session=self).room_name
            return ROOM_DICT[room_name]
        except RoomToSession.DoesNotExist:
            return None

    def _get_payoff_plus_participation_fee(self, payoff):
        '''For a participant who has the given payoff,
        return their payoff_plus_participation_fee
        Useful to define it here, for data export
        '''

        return (
            self.config['participation_fee'] +
            payoff.to_real_world_currency(self)
        )

    def _set_admin_report_app_names(self):

        admin_report_app_names = []
        num_rounds_list = []
        for app_name in self.config['app_sequence']:
            models_module = otree.common_internal.get_models_module(app_name)
            app_label = get_app_label_from_name(app_name)
            try:
                get_template('{}/AdminReport.html'.format(app_label))
                admin_report_app_names.append(app_name)
                num_rounds_list.append(models_module.Constants.num_rounds)
            except TemplateDoesNotExist:
                pass
        self._admin_report_app_names = ';'.join(admin_report_app_names)
        self._admin_report_num_rounds = ';'.join(str(n) for n in num_rounds_list)

    def _admin_report_apps(self):
        return self._admin_report_app_names.split(';')

    def _admin_report_num_rounds_list(self):
        return [int(num) for num in self._admin_report_num_rounds.split(';')]

    def has_admin_report(self):
        return bool(self._admin_report_app_names)
