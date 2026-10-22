import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('feedback.api:create_app', factory=True, host='0.0.0.0', port=int(os.getenv('PORT', '8000')),
                proxy_headers=bool(os.getenv('TRUSTED_PROXY_IPS')), forwarded_allow_ips=os.getenv('TRUSTED_PROXY_IPS', ''),
                access_log=False, ws_max_size=32768, ws_max_queue=16,
                ws_ping_interval=20, ws_ping_timeout=20)
