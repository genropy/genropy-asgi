"""Compare ordinary HTTP and WSK RPC without application database writes."""

from datetime import date
from decimal import Decimal

from gnr.core.gnrbag import Bag
from gnr.core.gnrlang import GnrSilentException, getUuid


class GnrCustomWebPage:
    def pageAuthTags(self, **kwargs):
        return ''

    def onIniting(self, request_args, request_kwargs):
        self.invocation_id = getUuid()
        self.clean_before = self.site.currentPage is None

    def main(self, root, **kwargs):
        root.div('Ephemeral page RPC: HTTP and WebSocket')
        for transport in ('POST', 'WSK'):
            root.button('Call ' + transport, action="""
                genro.rpc.remoteCall('probe', {value:7}, 'bag', transport, null,
                    function(result){
                        genro.setData('result_' + transport, result.toXml());
                    });
            """, transport=transport)
            root.div('^result_' + transport)
        root.button('Call error WSK', action="""
            genro.wsk.call({method:'fail'}).addCallback(function(result){
                genro.setData('error_result', JSON.stringify(result));
            });
        """)
        root.div('^error_result')

    def rpc_probe(self, value=None):
        return Bag(dict(value=value, value_type=type(value).__name__,
                        invocation_id=self.invocation_id, clean_before=self.clean_before,
                        current_page=self.site.currentPage is self,
                        day=date(2026, 9, 9), amount=Decimal('12.30')))

    def rpc_fail(self):
        raise GnrSilentException(topic='wsx_test', parameters={})

    def internal_only(self):
        raise AssertionError('Private method must not be remotely callable')
