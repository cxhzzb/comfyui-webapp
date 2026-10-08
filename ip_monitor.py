# -*- coding: utf-8 -*-
"""公网 IP 监控：公网 IP 变化时自动发邮件通知（收件人见 mail_config.json）。

原理：定时（默认每 5 分钟）从多个公开接口查询当前公网 IP，
与上次记录（_last_public_ip.txt）比较，变化则通过邮件 SMTP 发信。
邮箱配置放在 mail_config.json（smtp_pass 通常是邮箱授权码，不是登录密码）。
首次启动会先发一封确认邮件，之后仅 IP 变化时发信。
"""
import json
import smtplib
import sys
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

# Windows 控制台默认 cp950，中文 print 会崩，强制 UTF-8 输出
# （pythonw 无控制台时 stdout 为 None，需容错）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "_last_public_ip.txt"
LOG_FILE = BASE / "logs" / "ip_monitor.log"
CHECK_INTERVAL = 300  # 秒（5 分钟）

# 邮件里展示的访问端口，与 webapp 配置保持一致（config.json / WEBAPP_PORT）
try:
    from config import cfg as _webapp_cfg

    WEBAPP_PORT = _webapp_cfg.port
except Exception:  # 单独运行且 config 不可用时退回默认端口
    WEBAPP_PORT = 8800

# 邮箱配置：本目录 mail_config.json（见 mail_config.example.json）
CONFIG_CANDIDATES = [
    BASE / "mail_config.json",
]

IP_SOURCES = [
    "https://ifconfig.me/ip",
    "https://api.ipify.org",
    "https://ipinfo.io/ip",
    "https://api-ipv4.ip.sb/ip",
]


def _log(msg):
    """同时输出到控制台（若有）和日志文件，pythonw 后台运行也不丢日志。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


def load_config():
    for p in CONFIG_CANDIDATES:
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            missing = [k for k in ("smtp_user", "smtp_pass", "to") if not cfg.get(k)]
            if missing:
                sys.exit(f"{p} 缺少字段: {missing}")
            _log(f"[ipmon] 邮箱配置: {p}")
            return cfg
    sys.exit(f"找不到 mail_config.json（应放在 {BASE / 'mail_config.json'}）")


def get_public_ip():
    """依次尝试多个接口，返回公网 IPv4 字符串；全部失败返回 None。"""
    for url in IP_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                ip = r.read().decode("utf-8", "replace").strip()
            if ip and all(c.isdigit() or c == "." for c in ip):
                return ip
        except Exception:
            continue
    return None


def send_mail(cfg, new_ip, old_ip=None):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if old_ip:
        subject = f"公网 IP 已变化：{new_ip}"
        body = (
            f"你的公网 IP 已发生变化：\n\n"
            f"    新 IP：{new_ip}\n"
            f"    旧 IP：{old_ip}\n\n"
            f"变化时间：{now}\n"
            f"访问地址：http://{new_ip}:{WEBAPP_PORT}\n\n"
            f"（手机用流量访问 http://{new_ip}:{WEBAPP_PORT}，用网页账号登录）\n\n"
            f"—— ComfyUI webapp 公网 IP 监控"
        )
    else:
        subject = f"公网 IP 监控已启动：{new_ip}"
        body = (
            f"公网 IP 监控已启动，当前公网 IP：\n\n"
            f"    {new_ip}\n\n"
            f"时间：{now}\n"
            f"访问地址：http://{new_ip}:{WEBAPP_PORT}\n\n"
            f"之后公网 IP 每次变化都会自动发邮件通知你。\n\n"
            f"—— ComfyUI webapp 公网 IP 监控"
        )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["to"]
    with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.qq.com"), int(cfg.get("smtp_port", 465)), timeout=30) as s:
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.sendmail(cfg["smtp_user"], [cfg["to"]], msg.as_string())
    return subject


def main():
    cfg = load_config()
    last = STATE_FILE.read_text(encoding="utf-8").strip() if STATE_FILE.exists() else None
    _log(f"[ipmon] 启动，上次 IP: {last or '(无记录)'}，每 {CHECK_INTERVAL}s 检查一次")
    while True:
        try:
            ip = get_public_ip()
            if ip is None:
                _log(f"[ipmon] {time.strftime('%H:%M:%S')} 查询公网 IP 失败，跳过本轮")
            elif last is None:
                # 首次运行（无历史记录）：发一封确认邮件
                subject = send_mail(cfg, ip, None)
                STATE_FILE.write_text(ip, encoding="utf-8")
                _log(f"[ipmon] 首次启动确认，已发邮件: {subject}")
                last = ip
            elif ip != last:
                # IP 变化：发邮件
                subject = send_mail(cfg, ip, last)
                STATE_FILE.write_text(ip, encoding="utf-8")
                _log(f"[ipmon] IP 变化 {last} -> {ip}，已发邮件: {subject}")
                last = ip
            else:
                _log(f"[ipmon] {time.strftime('%H:%M:%S')} IP 未变化（{ip}）")
        except Exception as e:
            _log(f"[ipmon] 出错（继续运行）: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
