"""清爽部署脚本 — 上传代码 + 清理旧容器 + 重建"""
import paramiko, io, sys, os
sys.stdout.reconfigure(encoding='utf-8')

HOST = '124.220.55.169'
USER = 'ubuntu'

with open('ssh_key', 'r') as f:
    pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(f.read()))

def ssh_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, pkey=pkey, timeout=30, look_for_keys=False, allow_agent=False)
    return c

def run(cmd, timeout=30):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors='replace').strip()
    err = stderr.read().decode(errors='replace').strip()
    if err:
        print('  ERR:', err[:200])
    return out

print('=== 1. 连接服务器 ===')
client = ssh_connect()
print('OK')

print('\n=== 2. 停止并删除旧容器 ===')
run('sudo docker stop bid-app 2>/dev/null; sudo docker rm bid-app 2>/dev/null; echo done')

print('\n=== 3. 清理旧镜像 ===')
run('sudo docker rmi bid-monitor-img 2>/dev/null; echo done')

print('\n=== 4. 清理服务器上旧文件 ===')
run('rm -rf ~/bid-app && mkdir -p ~/bid-app')

print('\n=== 5. 上传代码 ===')
# SFTP upload
transport = paramiko.Transport((HOST, 22))
transport.connect(username=USER, pkey=pkey)
sftp = paramiko.SFTPClient.from_transport(transport)
for fname in ['app.py', 'index.html', 'Dockerfile', 'requirements.txt']:
    local = os.path.join(os.path.dirname(__file__), fname)
    remote = f'/home/{USER}/bid-app/{fname}'
    sftp.put(local, remote)
    print(f'  {fname} -> server')
sftp.close()
transport.close()

print('\n=== 6. Docker 构建 ===')
o = run('cd ~/bid-app && sudo docker build -t bid-app . 2>&1')
print(o[-300:])

print('\n=== 7. Docker 运行 (端口 80) ===')
o = run('sudo docker run -d --name bid-app --restart unless-stopped -p 80:5000 bid-app 2>&1')
print(o)

print('\n=== 8. 等待启动 + 验证 ===')
import time
time.sleep(3)
o = run('sudo docker ps --filter name=bid-app --format "{{.Names}} {{.Status}} {{.Ports}}"')
print(o)
o = run('curl -s -o /dev/null -w "%{http_code}" http://localhost:80')
print(f'localhost:80 -> HTTP {o}')

print('\n=== 9. 获取服务器数据 ===')
o = run('curl -s http://localhost:80/api/bids | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d), \"条数据\"); [print(f\"  [{b[chr(99)+chr(97)+chr(116)+chr(101)+chr(103)+chr(111)+chr(114)+chr(121)]}] {b[chr(116)+chr(105)+chr(116)+chr(108)+chr(101)][:50]}\") for b in d]"')
print(o)

client.close()

print('\n=== 完成 ===')
print('内网访问正常')
print('⚠ 外网访问需在腾讯云控制台放行 80 端口:')
print('  https://console.cloud.tencent.com/lighthouse/firewall')
