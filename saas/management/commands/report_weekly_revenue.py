# Copyright (c) 2023, DjaoDjin inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Command for the cron job. Send revenue report for the last week"""

import logging

from dateutil.relativedelta import relativedelta
from django.core.management.base import BaseCommand

from ... import settings
from ... import signals
from ...humanize import as_money
from ...metrics.base import (aggregate_transactions_by_period,
                             aggregate_transactions_change_by_period, get_different_units,
                             hour_periods, day_periods, week_periods, month_periods_v2, year_periods)
from ...models import Transaction, Plan
from ...utils import datetime_or_now, get_organization_model, parse_tz

LOGGER = logging.getLogger(__name__)

class Command(BaseCommand):
    """Send past week revenue report in email"""
    help = 'Send past week revenue report in email'

    def add_arguments(self, parser):
        parser.add_argument(
            '--at-time', action='store',
            dest='at_time', default=None,
            help='Specifies the time at which the command runs'
        )
        parser.add_argument(
            '--provider', action='append',
            dest='providers', default=None,
            help='Specifies provider to generate reports for.'
        )
        parser.add_argument(
            '--period', action='store',
            dest='period', default='weekly',
            choices=[choice[1].lower() for choice in Plan.INTERVAL_CHOICES],
            help='Specifies the period to generate reports for'
        )

    @staticmethod
    def construct_date_periods(at_time, period='weekly', timezone=None):
        # discarding time, keeping utc tzinfo (00:00:00 utc)
        tzinfo = parse_tz(timezone)

        def localize_time(time):
            # we are interested in 00:00 local time, if we don't have
            # local time zone, fall back to 00:00 utc time
            # in case we have local timezone, replace utc with it
            return tzinfo.localize(time.replace(tzinfo=None)) if tzinfo else time

        base_time = at_time.replace(
            minute=0 if period != 'yearly' else at_time.minute,
            second=0,
            microsecond=0
        )

        base_time = localize_time(base_time)

        # Map period types to corresponding date-generating functions.
        period_func = {
            'hourly': hour_periods,
            'daily': day_periods,
            'weekly': week_periods,
            'monthly': month_periods_v2,
            'yearly': year_periods,
        }.get(period)

        if not period_func:
            return None, None
        # 'yearly' is unique in its treatment of the starting date.
        # Flag set for special handling later.
        include_start = (period == 'yearly')

        # Calculate the date exactly one year prior to base_time;
        prev_year_from_date = base_time - relativedelta(years=1)

        # Logic for dealing with cases where it's the first day of the year
        if base_time.day == 1 and base_time.month == 1:
            prev_year_from_date -= relativedelta(years=1)
        # Generates the most recent two periods and the recent period from a year ago
        # using date-generating functions.
        prev_period = period_func(2, from_date=base_time, tz=tzinfo, include_start_date=include_start)
        prev_year = period_func(1, from_date=prev_year_from_date, tz=tzinfo, include_start_date=False)

        return prev_period, prev_year

    @staticmethod
    def construct_table(table, unit):
        def calculate_percentage_change(current, previous):
            try:
                amount = (current - previous) * 100 / previous
                percentage = str(round(amount, 2)) + '%'
                if amount > 0:
                    percentage = '+' + percentage
                return percentage
            except ZeroDivisionError:
                return 'N/A'

        for row in table:
            val = row['values']
            val['prev'] = calculate_percentage_change(val['last'], val['prev'])
            val['prev_year'] = calculate_percentage_change(val['last'], val['prev_year'])
            val['last'] = as_money(val['last'], unit)
        return table

    @staticmethod
    def get_perf_data(provider, prev_periods, prev_year_periods, period_type):
        # pylint:disable=too-many-locals
        account_table, _, _, table_unit = aggregate_transactions_change_by_period(
            provider, Transaction.RECEIVABLE, account_title='Sales',
            orig='orig', dest='dest', date_periods=prev_periods
        )
        account_table_prev_year, _, _, _ = aggregate_transactions_change_by_period(
            provider, Transaction.RECEIVABLE, account_title='Sales',
            orig='orig', dest='dest', date_periods=prev_year_periods
        )

        _, payment_amounts, payments_unit = aggregate_transactions_by_period(
            provider, Transaction.RECEIVABLE,
            orig='dest', dest='dest', orig_account=Transaction.BACKLOG,
            orig_organization=provider, date_periods=prev_periods
        )
        _, payment_amounts_prev_year, _ = aggregate_transactions_by_period(
            provider, Transaction.RECEIVABLE,
            orig='dest', dest='dest', orig_account=Transaction.BACKLOG,
            orig_organization=provider, date_periods=prev_year_periods
        )

        _, refund_amounts, refund_unit = aggregate_transactions_by_period(
            provider, Transaction.REFUND,
            orig='dest', dest='dest', date_periods=prev_periods
        )
        _, refund_amounts_prev_year, _ = aggregate_transactions_by_period(
            provider, Transaction.REFUND,
            orig='dest', dest='dest', date_periods=prev_year_periods
        )

        unit = settings.DEFAULT_UNIT
        units = get_different_units(table_unit, payments_unit, refund_unit)

        if len(units) > 1:
            LOGGER.error("different units in get_perf_data(period_type=%s): %s", period_type, units)

        if units:
            unit = units[0]
        table = [{
            'slug': "Total Sales",
            'title': "Total Sales",
            'values': {
                'last': account_table[0]['values'][1][1],
                'prev': account_table[0]['values'][0][1],
                'prev_year': account_table_prev_year[0]['values'][0][1]
            }}, {
            'slug': "New Sales",
            'title': "New Sales",
            'values': {
                'last': account_table[1]['values'][1][1],
                'prev': account_table[1]['values'][0][1],
                'prev_year': account_table_prev_year[1]['values'][0][1]
            }}, {
            'slug': "Churned Sales",
            'title': "Churned Sales",
            'values': {
                'last': account_table[2]['values'][1][1],
                'prev': account_table[2]['values'][0][1],
                'prev_year': account_table_prev_year[2]['values'][0][1]
            }}, {
            'slug': "Payments",
            'title': "Payments",
            'values': {
                'last': payment_amounts[1][1],
                'prev': payment_amounts[0][1],
                'prev_year': payment_amounts_prev_year[0][1]
            }}, {
            'slug': "Refunds",
            'title': "Refunds",
            'values': {
                'last': refund_amounts[1][1],
                'prev': refund_amounts[0][1],
                'prev_year': refund_amounts_prev_year[0][1]
            }}
        ]
        return (table, unit)

    def handle(self, *args, **options):
        # aware utc datetime object
        at_time = datetime_or_now(options.get('at_time'))
        period = options.get('period')

        self.stdout.write("running report_weekly_revenue for %s %s period at %s" %
                          ('an' if period == 'hourly' else 'a', period, at_time))

        providers = get_organization_model().objects.filter(is_provider=True)
        provider_slugs = options.get('providers')
        if provider_slugs:
            providers = providers.filter(slug__in=provider_slugs)
        for provider in providers:
            dates = self.construct_date_periods(
                at_time, period=period, timezone=provider.default_timezone)
            prev_period, prev_year = dates
            if period == 'yearly':
                self.stdout.write(
                    "Two last consecutive yearly periods\n: %s to %s and %s to %s" %
                    (prev_period[0].isoformat(), prev_period[1].isoformat(), prev_period[1].isoformat(),
                     prev_period[2].isoformat())
                )
                self.stdout.write(
                    "Year before the corresponding yearly period\n: %s to %s" %
                    (prev_year[0].isoformat(), prev_year[1].isoformat())
                )
            else:
                self.stdout.write(
                    "Two last consecutive %s periods\n: %s to %s and %s to %s" %
                    (period, prev_period[0].isoformat(), prev_period[1].isoformat(),
                     prev_period[1].isoformat(), prev_period[2].isoformat())
                )
                self.stdout.write(
                    "Same %s period from the previous year\n: %s to %s" %
                    (period, prev_year[0].isoformat(), prev_year[1].isoformat())
                )
            data, unit = self.get_perf_data(provider, prev_period, prev_year, period_type=period)
            table = self.construct_table(data, unit)
            signals.period_sales_report_created.send(sender=__name__, provider=provider, dates=dates, data=table)
