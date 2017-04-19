# Copyright notice:
#
# Copyright (C) CERN 2013-2015
#
# Copyright (C) Members of the EMI Collaboration, 2010-2013.
#   See www.eu-emi.eu for details on the copyright holders
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import types
from datetime import datetime, timedelta
from django.db import connection
from django.db.models import Count
from django.views.decorators.cache import cache_page

from authn import require_certificate
from ftsweb.models import File, OptimizeActive, OptimizerEvolution
from jobs import setup_filters
from jsonify import jsonify
from util import get_order_by, paged, db_to_date


def _get_pair_limits(limits, source, destination):
    pair_limits = {'source': dict(), 'destination': dict()}
    for l in limits:
        if l[0] == source:
            if l[2]:
                pair_limits['source']['bandwidth'] = l[2]
            elif l[3]:
                pair_limits['source']['active'] = l[3]
        elif l[1] == destination:
            if l[2]:
                pair_limits['destination']['bandwidth'] = l[2]
            elif l[3]:
                pair_limits['destination']['active'] = l[3]
    if len(pair_limits['source']) == 0:
        del pair_limits['source']
    if len(pair_limits['destination']) == 0:
        del pair_limits['destination']
    return pair_limits


def _get_pair_udt(udt_pairs, source, destination):
    for u in udt_pairs:
        if udt_pairs[0] == source and udt_pairs[1] == destination:
            return True
        elif udt_pairs[0] == source and not udt_pairs[1]:
            return True
        elif not udt_pairs[0] and udt_pairs[1] == destination:
            return True
    return False


def _seconds(td):
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10 ** 6) / 10 ** 6


class OverviewExtended(object):
    """
    Wraps the return of overview, so when iterating, we can retrieve
    additional information.
    This way we avoid doing it for all items. Only those displayed will be queried
    (i.e. paging)
    """

    def __init__(self, not_before, objects, cursor):
        self.objects = objects
        self.not_before = not_before
        self.cursor = cursor

    def __len__(self):
        return len(self.objects)

    def _get_fixed(self, source, destination):
        """
        Return true if the number of actives is fixed for this pair
        """
        oa = OptimizeActive.objects.filter(source_se=source, dest_se=destination).values('fixed').all()
        if len(oa):
            oa = oa[0]
            return oa['fixed'] is not None and oa['fixed'].lower == 'on'
        return False

    def _get_throughput(self, source, destination, vo):
        """
        Calculate throughput (in MB) over this pair + vo over the last minute
        """
        now = datetime.utcnow()
        window_size = 30
        window_start = now - timedelta(minutes=window_size)

        oe = OptimizerEvolution.objects.filter(source_se=source, dest_se=destination)\
            .values('throughput', 'filesize_avg', 'active')\
            .filter(datetime__gte=window_start)\
            .order_by('-datetime')[0:1]
        if len(oe) == 0:
            return 0, 0

        return oe[0]['active'] * oe[0]['filesize_avg'], oe[0]['throughput'] / 1024**2

    def __getitem__(self, indexes):
        if isinstance(indexes, types.SliceType):
            return_list = self.objects[indexes]
            for item in return_list:
                item['active_fixed'] = self._get_fixed(item['source_se'], item['dest_se'])
                item['transferred'], item['current'] = self._get_throughput(item['source_se'], item['dest_se'], item['vo_name'])
            return return_list
        else:
            return self.objects[indexes]


@require_certificate
@cache_page(60)
@jsonify
def get_overview(http_request):
    filters = setup_filters(http_request)
    if filters['time_window']:
        not_before = datetime.utcnow() - timedelta(hours=filters['time_window'])
    else:
        not_before = datetime.utcnow() - timedelta(hours=1)

    cursor = connection.cursor()

    # Get all pairs first
    pairs_filter = ""
    se_params = []
    if filters['source_se']:
        pairs_filter += " AND source_se = %s "
        se_params.append(filters['source_se'])
    if filters['dest_se']:
        pairs_filter += " AND dest_se = %s "
        se_params.append(filters['dest_se'])
    if filters['vo']:
        pairs_filter += " AND vo_name = %s "
        se_params.append(filters['vo'])

    # Result
    triplets = dict()

    # Non terminal
    query = """
    SELECT COUNT(file_state) as count, file_state, source_se, dest_se, vo_name
    FROM t_file
    WHERE file_state in ('SUBMITTED', 'ACTIVE', 'STAGING', 'STARTED') %s
    GROUP BY file_state, source_se, dest_se, vo_name order by NULL
    """ % pairs_filter
    cursor.execute(query, se_params)
    for row in cursor.fetchall():
        triplet_key = (row[2], row[3], row[4])
        triplet = triplets.get(triplet_key, dict())

        triplet[row[1].lower()] = row[0]

        triplets[triplet_key] = triplet

    # Terminal
    query = """
    SELECT COUNT(file_state) as count, file_state, source_se, dest_se, vo_name
    FROM t_file
    WHERE file_state in ('FINISHED', 'FAILED', 'CANCELED') %s
        AND finish_time > %s
    GROUP BY file_state, source_se, dest_se, vo_name  order by NULL
    """ % (pairs_filter, db_to_date())
    cursor.execute(query, se_params + [not_before])
    for row in cursor.fetchall():
        triplet_key = (row[2], row[3], row[4])
        triplet = triplets.get(triplet_key, dict())

        triplet[row[1].lower()] = row[0]

        triplets[triplet_key] = triplet

    # Limitations
    limit_query = "SELECT source_se, dest_se, throughput, active FROM t_optimize WHERE throughput IS NOT NULL or active IS NOT NULL"
    cursor.execute(limit_query)
    limits = cursor.fetchall()

    # UDT
    udt_query = "SELECT source_se, dest_se FROM t_optimize WHERE udt = 'on'"
    cursor.execute(udt_query)
    udt_pairs = cursor.fetchall()

    # Transform into a list
    objs = []
    for (triplet, obj) in triplets.iteritems():
        obj['source_se'] = triplet[0]
        obj['dest_se'] = triplet[1]
        obj['vo_name'] = triplet[2]
        if 'current' not in obj and 'active' in obj:
            obj['current'] = 0
        failed = obj.get('failed', 0)
        finished = obj.get('finished', 0)
        total = failed + finished
        if total > 0:
            obj['rate'] = (finished * 100.0) / total
        else:
            obj['rate'] = None
        # Append limit, if any
        obj['limits'] =_get_pair_limits(limits, triplet[0], triplet[1])
        # Mark UDT-enabled
        obj['udt'] = _get_pair_udt(udt_pairs, triplet[0], triplet[1])

        objs.append(obj)

    # Ordering
    (order_by, order_desc) = get_order_by(http_request)

    if order_by == 'active':
        sorting_method = lambda o: (o.get('active', 0), o.get('submitted', 0))
    elif order_by == 'finished':
        sorting_method = lambda o: (o.get('finished', 0), o.get('failed', 0))
    elif order_by == 'failed':
        sorting_method = lambda o: (o.get('failed', 0), o.get('finished', 0))
    elif order_by == 'canceled':
        sorting_method = lambda o: (o.get('canceled', 0), o.get('finished', 0))
    elif order_by == 'staging':
        sorting_method = lambda o: (o.get('staging', 0), o.get('started', 0))
    elif order_by == 'started':
        sorting_method = lambda o: (o.get('started', 0), o.get('staging', 0))
    elif order_by == 'rate':
        sorting_method = lambda o: (o.get('rate', 0), o.get('finished', 0))
    else:
        sorting_method = lambda o: (o.get('submitted', 0), o.get('active', 0))

    # Generate summary
    summary = {
        'submitted': sum(map(lambda o: o.get('submitted', 0), objs), 0),
        'active': sum(map(lambda o: o.get('active', 0), objs), 0),
        'finished': sum(map(lambda o: o.get('finished', 0), objs), 0),
        'failed': sum(map(lambda o: o.get('failed', 0), objs), 0),
        'canceled': sum(map(lambda o: o.get('canceled', 0), objs), 0),
        'current': sum(map(lambda o: o.get('current', 0), objs), 0),
        'staging': sum(map(lambda o: o.get('staging', 0), objs), 0),
        'started': sum(map(lambda o: o.get('started', 0), objs), 0),
    }
    if summary['finished'] > 0 or summary['failed'] > 0:
        summary['rate'] = (float(summary['finished']) / (summary['finished'] + summary['failed'])) * 100

    # Return
    return {
        'overview': paged(
            OverviewExtended(not_before, sorted(objs, key=sorting_method, reverse=order_desc), cursor=cursor),
            http_request
        ),
        'summary': summary
    }
