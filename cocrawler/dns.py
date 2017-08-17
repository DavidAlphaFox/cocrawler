'''
DNS-related code
'''

import time
import logging
import urllib
import ipaddress

import cachetools
import aiohttp
import aiodns

from . import stats
from . import config

LOGGER = logging.getLogger(__name__)


class CoCrawler_AsyncResolver(aiohttp.resolver.AsyncResolver):
    '''
    A dns wrapper that applies our policies

    TODO: subtract off dns time from fetch first byte time?
    TODO: Warc
    TODO: Use a different call so we can get the real TTL for the warc
    TODO: clear the cache so it's not unbounded in size
    TODO: use the real TTL?
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._crawllocalhost = config.read(('Fetcher', 'CrawlLocalhost')) or False
        self._crawlprivate = config.read(('Fetcher', 'CrawlPrivate')) or False

    async def resolve(self, host, port, **kwargs):
        with stats.record_latency('fetcher DNS lookup', url=host):
            with stats.coroutine_state('fetcher DNS lookup'):
                addrs = await super().resolve(host, port, **kwargs)

        ret = []
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a.host)
            except ValueError:
                continue
            if not self._crawllocalhost and ip.is_localhost:
                continue
            if not self._crawlprivate and ip.is_private:
                continue
            ret.append(a)

        if len(addrs) != len(ret):
            LOGGER.info('threw out some ip addresses for %s', host)

        return ret


class CoCrawler_Caching_AsyncResolver(aiohttp.resolver.AsyncResolver):
    '''
    A caching dns wrapper that lets us subvert aiohttp's built-in dns policies

    Use a LRU cache which respects TTL and is bounded in size.
    Set a "dns nap" while doing a fetch.
    Refetch dns (once!) when the TTL is 9/10ths expired.

    TODO: subtract off dns time from fetch first byte time?
    TODO: Warc
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._crawllocalhost = config.read(('Fetcher', 'CrawlLocalhost')) or False
        self._crawlprivate = config.read(('Fetcher', 'CrawlPrivate')) or False
        self._cachemaxsize = config.read(('Fetcher', 'DNSCacheMaxSize'))
        self._cache = cachetools.LRUCache(self._cachemaxsize)
        self._refresh_in_progress = set()
        LOGGER.error('hey greg init called')

    async def resolve(self, host, port, **kwargs):
        t = time.time()
        if host in self._cache:
            LOGGER.error('hey greg initial hit in cache for %s', host)
            (addrs, expires, refresh) = self._cache[host]
            if expires < t:
                LOGGER.error('hey greg expired for %s', host)
                del self._cache[host]
            elif refresh < t and host not in self._refresh_in_progress:
                # TODO: spawn a thread to await this while I continue on
                self._refresh_in_progress.add(host)
                LOGGER.error('hey greg refreshing %s', host)
                self._cache[host] = await self.actual_async_lookup(host)
                self._refresh_in_progress.remove(host)

        if host not in self._cache:
            LOGGER.error('hey greg in the end, a miss for %s', host)
            self._cache[host] = await self.actual_async_lookup(host)

        (addrs, _, _) = self._cache[host]
        return addrs[0].host

    async def actual_async_lookup(self, host):
        '''
        Do an actual lookup. Always raise if it fails.
        '''
        LOGGER.error('hey greg actual lookup for %s', host)
        with stats.record_latency('fetcher DNS lookup', url=host):
            with stats.coroutine_state('fetcher DNS lookup'):
                # XXX TODO: how should I deal with AAAA vs A?
                LOGGER.error('hey greg query started')
                addrs = await query(host, 'A')
                LOGGER.error('hey greg query got %r', addrs)

        # filter return value to exclude unwanted ip addrs
        ret = []
        ttl = 0
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a.host)
            except ValueError:
                continue
            if not self._crawllocalhost and ip.is_localhost:
                continue
            if not self._crawlprivate and ip.is_private:
                continue
            ret.append(a)
            ttl = a.ttl  # all should be equal, we'll remember the last

        if len(ret) == 0:
            raise ValueError

        ttl = max(3600*8, min(3600, ttl))  # force ttl into a range of time
        t = time.time()
        expires = t + ttl
        refresh = t + (ttl * 0.75)

        if len(addrs) != len(ret):
            LOGGER.info('threw out some ip addresses for %s', host)

        LOGGER.error('actual lookup, returning %r', ret)

        return ret, expires, refresh


def get_resolver_wrapper(**kwargs):
    return CoCrawler_Caching_AsyncResolver(**kwargs)


'''
Code below is not actually used by crawling
'''


async def prefetch_dns(url, mock_url, session):
    '''
    So that we can track DNS transactions, and log them, we try to make sure
    DNS answers are in the cache before we try to fetch from a host that's not cached.

    TODO: Note that this TCPConnector's cache never expires, so we need to clear it occasionally.
    TODO: make multiple source IPs work. Alas this is submerged into pycares.Channel.set_local_ip() :-(
    TODO: https://developers.google.com/speed/public-dns/docs/dns-over-https -- optional plugin?
    TODO: if there are multiple A's, let's make sure they get saved and get used

    Note comments about google crawler at https://developers.google.com/speed/public-dns/docs/performance
    RR types A=1 AAAA=28 CNAME=5 NS=2
    The root of a domain cannot have CNAME. NS records are only in the root. These rules are not directly enforced
    Query for A when it's a CNAME comes back with answer list CNAME -> ... -> A,A,A...
    If you see a CNAME there should be no NS
    NS records can lie, but, it seems that most hosting companies use 'em "correctly"
    '''
    if mock_url is None:
        netloc_parts = url.urlsplit.netloc.split(':', maxsplit=1)
    else:
        mockurl_parts = urllib.parse.urlsplit(mock_url)
        netloc_parts = mockurl_parts.netloc.split(':', maxsplit=1)
    host = netloc_parts[0]
    try:
        port = int(netloc_parts[1])
    except IndexError:
        port = 80

    answer = None
    iplist = []

    if (host, port) not in session.connector.cached_hosts:
        with stats.record_latency('fetcher DNS lookup', url=host):
            with stats.coroutine_state('fetcher DNS lookup'):
                # we want to use this protected thing because we want the result cached in the connector
                answer = await session.connector._resolve_host(host, port)  # pylint: disable=protected-access
                stats.stats_sum('DNS prefetches', 1)
    else:
        answer = session.connector.cached_hosts[(host, port)]

    # XXX log DNS result to warc here?
    #  we should still log the IP to warc even if private
    #  note that these results don't have the TTL in them -- need to run query() to get that
    #  CNAME? -- similar to TTL

    for a in answer:
        ip = a['host']
        if mock_url is None and ipaddress.ip_address(ip).is_private:
            LOGGER.info('host %s has private ip of %s, ignoring', host, ip)
            continue
        if ':' in ip:  # is this a valid sign of ipv6? XXX policy
            LOGGER.info('host %s has ipv6 result of %s, ignoring', host, ip)
            continue
        iplist.append(ip)

    if len(iplist) == 0:
        LOGGER.info('host %s has no addresses', host)

    return iplist

res = None


def setup_resolver(ns):
    global res
    res = aiodns.DNSResolver(nameservers=ns, rotate=True)


async def query(host, qtype):
    '''
    Use aiodns.query() to fetch dns info

    Example results:

    A: [ares_query_simple_result(host='172.217.26.206', ttl=108)]
    AAAA: [ares_query_simple_result(host='2404:6800:4007:800::200e', ttl=299)]
    NS: [ares_query_ns_result(host='ns2.google.com', ttl=None),
         ares_query_ns_result(host='ns4.google.com', ttl=None),
         ares_query_ns_result(host='ns1.google.com', ttl=None),
         ares_query_ns_result(host='ns3.google.com', ttl=None)]
    CNAME: ares_query_cname_result(cname='blogger.l.google.com', ttl=None)

    Alas, querying for A www.blogger.com doesn't return both the CNAME and the next A, just the final A.
    dig shows CNAME and A. aiodns / pycares doesn't seem to ever show the full info.
    '''
    if not res:
        raise RuntimeError('no nameservers configured')

    return await res.query(host, qtype)


def ip_to_geoip(ip):
    # given an ip, compute the geoip, ASN, ISP, and proxy (like Tor)
    # also do Google cse, Amazon AWS
    # real google ips are available from SPF, gce isn't in that list
    # amazon ec2 occasionally publishes a webpage with addrs, corporate not in that list
    # can I classify CloudFlare by ASN?
    # current MaxMind has an "anonymous ip" db: vpn, hosting provider, public proxy, tor exit node

    # MaxMind database pricing: Country $24/mo, City $100/mo, isp+asn $24/mo
    #   anonymous $call, looks like it's mostly useful for client IPs not webserver IPs
    # Free data: GeoLite2 Country and City, ASN (less accurate geos)

    # suggested cleanup of country_name:
    #  s/,( United)? Republic of$//
    #  s/Russian Federation/Russia/
    #  s/\bOf\b/of/
    # city sometimes equals country_name, should drop city then
    # strings sometimes have control characters in them
    # strings sometimes have latin 1 in them, sometimes utf-8
    # asn comes back as ASd+ \s+ isp

    pass
