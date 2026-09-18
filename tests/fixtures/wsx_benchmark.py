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
        root.div('HTTP / WSK latency benchmark')
        root.div('12 warm-up triplets, then 3 rounds of 50 rotating triplets. POST / fetch / WSK.')
        root.button('Run benchmark', action="""
            if(genro.getData('benchmark_running')){return;}
            genro.setData('benchmark_running', true);
            genro.setData('benchmark_summary', 'Running...');
            var samples = [];
            function invoke(transport, round, warmup){
                return new Promise(function(resolve,reject){
                    var start = performance.now();
                    var timer = setTimeout(function(){reject(new Error('Call timed out'));},20000);
                    var complete = function(result){
                            clearTimeout(timer);
                            var elapsed = performance.now()-start;
                            if(!result || result.error || result.getItem('value')!==7){
                                reject(new Error('Unexpected result on '+transport)); return;
                            }
                            var xhr = genro._last_rpc.ioArgs.xhr;
                            var server = Number(xhr.getResponseHeader('X-GnrTime'))*1000;
                            if(!warmup){samples.push({transport:transport,round:round,total_ms:elapsed,server_ms:server});}
                            resolve();
                        };
                    if(transport==='FETCH'){
                        var content = {method:'probe',value:7,page_id:genro.page_id};
                        var args = {content:content};
                        genro.rpc.register_call(args);
                        var serialized=genro.rpc.serializeParameters(content);
                        var body=Object.keys(serialized).map(function(k){return encodeURIComponent(k)+'='+encodeURIComponent(serialized[k]);}).join('&');
                        fetch(genro.rpc.pageIndexUrl(),{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest'},body:body})
                            .then(async function(response){
                                var xml=new DOMParser().parseFromString(await response.text(),'text/xml');
                                var ioArgs={args:args,url:genro.rpc.pageIndexUrl(),xhr:{getResponseHeader:function(n){return response.headers.get(n);}}};
                                complete(genro.rpc.resultHandler(xml,ioArgs));
                            }).catch(function(error){clearTimeout(timer);reject(error);});
                        return;
                    }
                    var deferred=genro.rpc.remoteCall('probe',{value:7},'bag',transport,null,complete);
                    if(deferred && deferred.addErrback){deferred.addErrback(function(error){clearTimeout(timer);reject(error);});}
                });
            }
            function summarize(rows){
                function stats(values){
                    values.sort(function(a,b){return a-b;});
                    return {n:values.length,median_ms:values.length%2?values[(values.length-1)/2]:(values[values.length/2-1]+values[values.length/2])/2,
                            p95_ms:values[Math.ceil(values.length*.95)-1],mean_ms:values.reduce(function(a,b){return a+b;},0)/values.length};
                }
                var out={};
                ['POST','FETCH','WSK'].forEach(function(t){var r=rows.filter(function(v){return v.transport===t;});
                    out[t]={total:stats(r.map(function(v){return v.total_ms;})),server:stats(r.map(function(v){return v.server_ms;}))};});
                return out;
            }
            (async function(){
                await genro.wsk.channelReady;
                for(var i=0;i<12;i++){await invoke('POST',0,true);await invoke('FETCH',0,true);await invoke('WSK',0,true);}
                for(var round=1;round<=3;round++){
                    for(var i=0;i<50;i++){
                        var order=['POST','FETCH','WSK']; var shift=(i+round)%3; order=order.slice(shift).concat(order.slice(0,shift));
                        for(var t of order){await invoke(t,round,false);}
                    }
                }
                var result={all:summarize(samples),rounds:[1,2,3].map(function(r){return summarize(samples.filter(function(s){return s.round===r;}));})};
                genro.setData('benchmark_summary',JSON.stringify(result,null,2));
                genro.setData('benchmark_samples',JSON.stringify(samples));
            })().catch(function(error){genro.setData('benchmark_summary','ERROR: '+error);})
                .finally(function(){genro.setData('benchmark_running',false);});
        """)
        root.pre('^benchmark_summary', nodeId='benchmark_summary')
        root.textarea(value='^benchmark_samples', lbl='Raw samples', width='95%', height='80px',
                      nodeId='benchmark_samples')

    def rpc_probe(self, value=None):
        return Bag(dict(value=value, value_type=type(value).__name__,
                        invocation_id=self.invocation_id, clean_before=self.clean_before,
                        current_page=self.site.currentPage is self,
                        day=date(2026, 9, 9), amount=Decimal('12.30')))

    def rpc_fail(self):
        raise GnrSilentException(topic='wsx_test', parameters={})

    def internal_only(self):
        raise AssertionError('Private method must not be remotely callable')
