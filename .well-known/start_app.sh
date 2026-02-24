#!/bin/bash
./start_mcp.sh &
sleep 2
exec gunicorn --worker-class gevent -w 1 -b 0.0.0.0:${PORT:-8080} main:app
```

Then update `.replit` deployment section to:
```
run = ["bash", "start_app.sh"]
```

Then republish.

Or — honestly the cleanest move since Railway handles MCP — just skip the script entirely and tell Replit to set the deployment run command to:
```
run = ["gunicorn", "--worker-class", "gevent", "-w", "1", "-b", "0.0.0.0:8080", "main:app"]