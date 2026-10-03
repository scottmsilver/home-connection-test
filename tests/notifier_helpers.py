def summary_policy():
    def branch(model, provider, request, total, price):
        return dict(model=model, providers=[provider], request_timeout=request, total_timeout=total, max_price=price)
    zero = dict(prompt=0, completion=0, request=0)
    return dict(free=[branch('example/primary:free','provider-a',15,30,zero), branch('example/secondary:free','provider-b',15,30,zero)], paid=branch('example/paid','provider-c',10,15,dict(prompt=.1,completion=.4,request=0)), hedge_delay=3, race_timeout=35, zdr=True, data_collection='deny')

def notifier_config(tmp_path):
    return dict(secrets=dict(telegram_token='TEST_BOT', telegram_chat_id='TEST_CHAT', webhook_token='TEST_WEBHOOK', grafana_token='TEST_VIEWER', openrouter_key='TEST_MODEL'), delivery_database=str(tmp_path/'delivery.sqlite'), queue_database=str(tmp_path/'queue.sqlite'), listen=dict(host='127.0.0.1',port=8092), renderer=dict(dashboard='sample-dashboard',panels=['1','2'],port=3000), summary=summary_policy(), queue=dict(capacity=1000,retry_delay=30))
