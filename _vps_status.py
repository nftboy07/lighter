import paramiko
import _vps_deploy as d

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(d.HOST, username=d.USER, password=d.password(), timeout=20, allow_agent=False, look_for_keys=False)
d.run(c, "cmd /c tasklist | findstr /I python")
d.run(c, r"powershell -NoProfile -Command Get-Content C:\LighterBot\sniper.log -Tail 50")
c.close()
