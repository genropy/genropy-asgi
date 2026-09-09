"""Temporary site page for reception-only WebSocket verification."""


class GnrCustomWebPage:
    def pageAuthTags(self, **kwargs):
        return ''

    def main(self, root, **kwargs):
        root.div('WebSocket reception test')
        root.button('Send websocket call', action="""
            genro.wsk.channelReady.then(function(){
                return genro.wsk.request('/_websocket_receive', {
                    method:'wsx_probe_must_not_execute', parameters:{value:'probe'}
                });
            }).then(function(result){
                    genro.setData('probe_result',JSON.stringify(result));
                }).catch(function(error){
                    genro.setData('probe_result','ERROR: '+error);
                });
        """)
        root.div('^probe_result')

    def rpc_wsx_probe_must_not_execute(self, **kwargs):
        raise AssertionError('Reception-only call reached the legacy dispatcher')
