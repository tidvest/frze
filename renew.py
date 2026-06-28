#!/usr/bin/env python3

import os
import re
import sys
import json
import time
import base64
import random
import traceback
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from pathlib import Path

# CloakBrowser 源码级指纹伪装，与 runfc 完全一致
from cloakbrowser import launch

DISCORD_TOKEN = os.environ.get("FREEZEHOST_DISCORD_TOKEN", "").strip()
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "").strip()

# 代理：Xray 本地 SOCKS5（与 runfc 完全一致）
PROXY_SERVER = "socks5://127.0.0.1:10808"

TIMEOUT          = 60_000
MAX_SITE_RETRIES = 3
RETRY_WAIT       = 30_000
SCREENSHOT_DIR   = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

BASE_URL   = "https://free.freezehost.pro"
VIEWPORT_W = 1280
VIEWPORT_H = 753

_SENSITIVE_VALUES: set[str] = set()
_SERVER_INDEX: dict[str, int] = {}


def _register_sensitive(*values):
    for v in values:
        if v and len(v) > 2:
            _SENSITIVE_VALUES.add(v)


def _server_label(server_id: str) -> str:
    if server_id not in _SERVER_INDEX:
        _SERVER_INDEX[server_id] = len(_SERVER_INDEX) + 1
    return f"服务器#{_SERVER_INDEX[server_id]}"


def _mask(text: str) -> str:
    if DISCORD_TOKEN:
        text = text.replace(DISCORD_TOKEN, "***")
    if TG_BOT_TOKEN:
        text = text.replace(TG_BOT_TOKEN, "***")
    if TG_CHAT_ID:
        text = text.replace(TG_CHAT_ID, "***")
    for val in _SENSITIVE_VALUES:
        if val in text:
            text = text.replace(val, "***")
    for sid, idx in _SERVER_INDEX.items():
        if sid in text:
            text = text.replace(sid, f"服务器#{idx}")
    text = re.sub(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.)\d{1,3}\b", r"\1xx", text)
    text = re.sub(r"connect\.sid=[^;\s]+", "connect.sid=***", text)
    return text


def log_info(msg: str):  print(f"[INFO] {_mask(msg)}")
def log_warn(msg: str):  print(f"[WARN] {_mask(msg)}")
def log_error(msg: str): print(f"[ERROR] {_mask(msg)}")



def wait_for_page_settle(page, settle_timeout=12) -> None:
    """等待 domcontentloaded 之后页面内容真正就绪"""
    deadline = time.time() + settle_timeout
    while time.time() < deadline:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        if len(body.strip()) > 100:
            log_info("  页面已稳定（内容就绪）")
            return
        time.sleep(0.5)
    log_info("  页面稳定等待超时，继续执行...")


def navigate(page, url, timeout=60) -> bool:
    """导航到目标页面（站点无 CF 验证，纯 Discord OAuth 登录）"""
    log_info(f"导航到: {url}")
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log_warn(f"goto 超时/异常: {e}，继续等待...")

    wait_for_page_settle(page, settle_timeout=12)
    return True


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def take_screenshot(page, name: str) -> bytes | None:
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOT_DIR / f"{ts}_{name}.png"
        page.screenshot(path=str(path), full_page=False)
        log_info(f"📸 截图: {path}")
        return path.read_bytes()
    except Exception as e:
        log_warn(f"截图失败: {e}")
        return None


def merge_screenshots(browser, buffers: list[bytes]) -> bytes | None:
    if not buffers:
        return None
    log_info("合并截图...")
    pg = browser.new_page()
    try:
        imgs = "".join(
            f'<img src="data:image/png;base64,{base64.b64encode(b).decode()}" '
            f'style="width:100%;border-radius:8px;border:2px solid #202225;'
            f'box-shadow:0 4px 6px rgba(0,0,0,.3);" />'
            for b in buffers
        )
        pg.set_content(
            f'<body style="margin:0;padding:15px;background:#2f3136;'
            f'display:flex;flex-direction:column;gap:15px;">{imgs}</body>'
        )
        time.sleep(0.5)
        return pg.screenshot(full_page=True)
    except Exception as e:
        log_warn(f"截图合并失败: {e}")
        return None
    finally:
        pg.close()


def parse_remaining(text: str) -> str | None:
    if not text:
        return None
    d = re.search(r"(\d+(?:\.\d+)?)\s*day", text, re.I)
    h = re.search(r"(\d+(?:\.\d+)?)\s*hour", text, re.I)
    days_raw  = float(d.group(1)) if d else 0.0
    hours_raw = float(h.group(1)) if h else 0.0
    extra_hours = (days_raw - int(days_raw)) * 24
    total_hours = hours_raw + extra_hours
    final_days  = int(days_raw)
    final_hours = int(total_hours)
    final_mins  = int(round((total_hours - final_hours) * 60))
    parts = []
    if final_days > 0:
        parts.append(f"{final_days}天")
    if final_hours > 0 or final_days > 0:
        parts.append(f"{final_hours}时")
    parts.append(f"{final_mins}分")
    return "".join(parts) if parts else None


def remaining_total_days(text: str) -> float | None:
    if not text:
        return None
    d = re.search(r"(\d+(?:\.\d+)?)\s*day", text, re.I)
    h = re.search(r"(\d+(?:\.\d+)?)\s*hour", text, re.I)
    days  = float(d.group(1)) if d else 0.0
    hours = float(h.group(1)) if h else 0.0
    return days + hours / 24.0


def send_tg(caption: str, image_bytes: bytes | None = None):
    if not TG_CHAT_ID or not TG_BOT_TOKEN:
        log_warn("TG 未配置，跳过推送")
        return
    try:
        if image_bytes:
            boundary = f"----Boundary{abs(hash(caption))}"
            body_parts = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"{caption}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="photo"; filename="s.png"\r\n'
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body_parts,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": TG_CHAT_ID, "text": caption}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urlopen(req, timeout=30) as resp:
            log_info("TG 推送成功" if resp.status == 200 else f"TG 推送失败: HTTP {resp.status}")
    except Exception as e:
        log_warn(f"TG 推送异常: {e}")


# ── FreezeHost 特有逻辑 ───────────────────────────────────────────────────────
def check_site_down(page) -> bool:
    try:
        return page.evaluate("""() => {
            const body = document.body ? document.body.innerText : '';
            if (body.includes('CONNECTION TO THE MANAGEMENT SERVICES LOST')) return true;
            if (body.includes('Retrying in') && body.includes('Retry Now')) return true;
            if (document.querySelector('button:has-text("Retry Now")')) return true;
            return false;
        }""")
    except Exception:
        return False


def dismiss_cookie_consent(page, timeout=8) -> bool:
    """
    关闭 GDPR/广告联盟 Cookie 同意弹窗（常见于荷兰语/多语言 CMP 浮层）。
    优先点"同意"类按钮，避免遮罩挡住后续的登录按钮。
    """
    consent_texts = [
        "Toestemming geven", "Akkoord", "Accepteren",
        "Accept all", "Accept", "I Agree", "I agree", "Agree",
        "同意", "接受", "Allow all", "Allow",
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for txt in consent_texts:
            try:
                btn = page.locator(f'button:has-text("{txt}")').first
                if btn.is_visible(timeout=300):
                    btn.click(timeout=1000)
                    log_info(f"已关闭 Cookie 同意弹窗（按钮: {txt}）")
                    time.sleep(0.5)
                    return True
            except Exception:
                continue
        time.sleep(0.3)
    log_info("未检测到 Cookie 同意弹窗（或已自行关闭）")
    return False


def wait_for_site_ready(page) -> bool:
    for attempt in range(1, MAX_SITE_RETRIES + 1):
        log_info(f"加载 FreezeHost 首页 (尝试 {attempt}/{MAX_SITE_RETRIES})...")
        try:
            ok = navigate(page, BASE_URL)
            if not ok:
                log_warn(f"页面加载未成功 (尝试 {attempt})")
                if attempt < MAX_SITE_RETRIES:
                    time.sleep(RETRY_WAIT / 1000)
                continue
        except Exception as e:
            log_warn(f"首页加载超时/异常 (尝试 {attempt}): {e}")
            if attempt < MAX_SITE_RETRIES:
                time.sleep(RETRY_WAIT / 1000)
            continue

        time.sleep(3)

        if check_site_down(page):
            log_warn(f"FreezeHost 后端服务不可用 (尝试 {attempt})")
            take_screenshot(page, f"site-down-{attempt}")
            try:
                retry_btn = page.locator('button:has-text("Retry Now")')
                if retry_btn.is_visible():
                    log_info("点击页面 Retry Now 按钮...")
                    retry_btn.click()
                    time.sleep(10)
                    if not check_site_down(page):
                        log_info("站点恢复正常")
                        return True
            except Exception:
                pass
            if attempt < MAX_SITE_RETRIES:
                log_info(f"等待 {RETRY_WAIT // 1000} 秒后重试...")
                time.sleep(RETRY_WAIT / 1000)
            continue

        try:
            login_visible = page.locator('span.text-lg:has-text("Login with Discord")').is_visible()
            if login_visible:
                log_info("首页加载正常，登录按钮可见")
                return True
        except Exception:
            pass

        log_info("首页已加载（未检测到宕机页面）")
        return True

    return False


def handle_oauth_page(page):
    log_info("进入 OAuth 授权页处理")
    time.sleep(2)

    for _ in range(20):
        if "discord.com" not in page.url:
            return
        btn_text = ""
        try:
            for sel in ['button[type="submit"]', 'div[class*="footer"] button', 'button[class*="primary"]']:
                btn = page.locator(sel).last
                if btn.is_visible():
                    btn_text = btn.inner_text().strip().lower()
                    break
        except Exception:
            pass
        if "authorize" in btn_text and "scroll" not in btn_text:
            break
        page.evaluate("""() => {
            const sels = ['[class*="scroller"]','[class*="oauth2"]','[class*="permissionList"]',
                '[class*="content"] [class*="scroll"]','[class*="listScroller"]',
                'div[class*="modal"] div[style*="overflow"]','div[class*="root"] div[style*="overflow"]'];
            let scrolled = false;
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const s = getComputedStyle(el);
                    if (el.scrollHeight > el.clientHeight &&
                        ['auto','scroll'].some(v => s.overflowY === v || s.overflow === v))
                        { el.scrollTop = el.scrollHeight; scrolled = true; }
                }
            }
            if (!scrolled) document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) {
                    const s = getComputedStyle(el);
                    if (['auto','scroll','hidden'].includes(s.overflowY)) el.scrollTop = el.scrollHeight;
                }
            });
            scrollTo(0, document.body.scrollHeight);
        }""")
        time.sleep(0.8)

    for _ in range(10):
        if "discord.com" not in page.url:
            return
        for sel in ['button:has-text("Authorize")', 'button:has-text("授权")',
                    'button[type="submit"]', 'div[class*="footer"] button', 'button[class*="primary"]']:
            try:
                btn = page.locator(sel).last
                if not btn.is_visible():
                    continue
                text = btn.inner_text().strip()
                if any(k in text.lower() for k in ("取消", "cancel", "deny")):
                    continue
                if "scroll" in text.lower():
                    page.evaluate("""() => {
                        document.querySelectorAll('div').forEach(el => {
                            if (el.scrollHeight > el.clientHeight + 5) el.scrollTop = el.scrollHeight;
                        }); scrollTo(0, document.body.scrollHeight);
                    }""")
                    time.sleep(1)
                    break
                if btn.is_disabled():
                    time.sleep(1)
                    break
                btn.click()
                time.sleep(2)
                if "discord.com" not in page.url:
                    return
                break
            except Exception:
                continue
        time.sleep(1.5)


def extract_email(page) -> str | None:
    try:
        log_info("打开 Settings 页面获取邮箱...")
        navigate(page, f"{BASE_URL}/settings")
        time.sleep(3)
        email = page.evaluate(r"""() => {
            const labels = document.querySelectorAll('p');
            for (const label of labels) {
                if (label.textContent.trim().toLowerCase().includes('email address')) {
                    const next = label.nextElementSibling;
                    if (next) {
                        const text = next.textContent.trim();
                        if (text.includes('@')) return text;
                    }
                }
            }
            const body = document.body.innerText;
            const m = body.match(/[\w.+-]+@[\w.-]+\.\w+/);
            return m ? m[0] : null;
        }""")
        if email:
            _register_sensitive(email)
            log_info(f"邮箱获取成功: {email}")
            return email
        log_warn("Settings 页面未找到邮箱")
        return None
    except Exception as e:
        log_warn(f"获取邮箱失败: {e}")
        return None


def discover_server_ids(page) -> list[str]:
    for attempt in range(3):
        captured: set[str] = set()

        def on_req(req):
            m = re.search(r"/api/server(?:resources|network|subdomain)\?id=([a-f0-9]+)", req.url, re.I)
            if m:
                captured.add(m.group(1))

        page.on("request", on_req)
        if attempt == 0:
            log_info("加载 Dashboard 发现服务器...")
            navigate(page, f"{BASE_URL}/dashboard")
        else:
            log_info(f"第 {attempt + 1} 次重试...")
            page.reload(wait_until="networkidle")

        time.sleep(5)
        page.remove_listener("request", on_req)

        js_ids = page.evaluate(r"""() => {
            const ids = [];
            if (typeof serverData !== 'undefined' && Array.isArray(serverData))
                serverData.forEach(s => { if (s.identifier) ids.push(s.identifier); });
            if (!ids.length) document.querySelectorAll('script:not([src])').forEach(sc => {
                for (const m of sc.textContent.matchAll(/identifier:\s*["']([a-f0-9]{6,})["']/gi))
                    ids.push(m[1]);
            });
            return ids;
        }""")

        all_ids = set(js_ids or []) | (captured if not js_ids else set())
        for sid in sorted(all_ids):
            _server_label(sid)
            _register_sensitive(sid)

        if all_ids:
            log_info(f"发现 {len(all_ids)} 台服务器")
            return sorted(all_ids)

        log_warn(f"第 {attempt + 1} 次未发现服务器")
        take_screenshot(page, f"dashboard-empty-{attempt + 1}")
        if attempt < 2:
            time.sleep(3)

    return []


def process_server(page, server_id: str) -> dict:
    server_url = f"{BASE_URL}/server-console?id={server_id}"
    result = dict(server_id=server_id, status="unknown", before=None, after=None,
                  emoji="❓", status_label="未知", detail="")

    log_info(f"[{server_id}] 开始处理")
    try:
        navigate(page, server_url)
        time.sleep(3)

        status_text = page.evaluate("""() => {
            const ids = ['renewal-status-console', 'server-timer-status'];
            for (const id of ids) {
                const el = document.getElementById(id);
                if (el && el.innerText.trim()) return el.innerText.trim();
            }
            return null;
        }""")
        log_info(f"[{server_id}] 续期状态: {status_text or '(空)'}")

        remaining_before = parse_remaining(status_text)
        total_days = remaining_total_days(status_text)
        result["before"] = remaining_before

        if total_days is not None and total_days > 7:
            log_info(f"[{server_id}] 剩余 {total_days:.1f} 天，无需续期")
            result.update(status="cooldown", emoji="⏳", status_label="冷却期",
                          detail=remaining_before or f"{total_days:.1f}天")
            return result

        # 查找续期链接
        renew_href = page.evaluate("""() => {
            const rl = document.getElementById('renew-link-modal');
            if (rl) { const h = rl.getAttribute('href'); if (h && h !== '#') return {href:h, text:rl.innerText.trim()}; }
            for (const a of document.querySelectorAll('a[href*="renew"]')) {
                const h = a.getAttribute('href');
                if (h && h.includes('renew') && h !== '#') return {href:h, text:a.innerText.trim()};
            }
            return null;
        }""")

        if not (renew_href and renew_href.get("href")):
            page.evaluate("""() => {
                const trigger = document.getElementById('renew-link-trigger')
                    || document.querySelector('[onclick*="showRenewalInfo"]')
                    || document.querySelector('i.fa-external-link-alt');
                if (trigger) { (trigger.closest('button') || trigger).click(); return; }
                if (typeof reviewAction === 'function') reviewAction('done');
            }""")
            time.sleep(2)
            renew_href = page.evaluate("""() => {
                const rl = document.getElementById('renew-link-modal');
                if (rl) { const h = rl.getAttribute('href'); if (h && h !== '#') return {href:h, text:rl.innerText.trim()}; }
                for (const a of document.querySelectorAll('a[href*="renew"]')) {
                    const h = a.getAttribute('href');
                    if (h && h.includes('renew') && h !== '#') return {href:h, text:a.innerText.trim()};
                }
                return null;
            }""")

        if not (renew_href and renew_href.get("href")):
            renew_href = page.evaluate(r"""() => {
                const m = document.body.innerHTML.match(/href=["']((?:\.\.)?\/renew\?id=[a-f0-9]+)["']/i);
                return m ? {href:m[1], text:'html-extract'} : null;
            }""")

        if not (renew_href and renew_href.get("href")):
            diag_html = page.evaluate("""() => {
                const candidates = [
                    document.getElementById('renew-link-modal'),
                    document.getElementById('renewal-info'),
                    document.querySelector('[id*="modal"]'),
                    document.querySelector('[class*="modal"]:not([class*="hidden"])'),
                ];
                for (const c of candidates) { if (c) return c.outerHTML.slice(0, 3000); }
                return null;
            }""")
            if diag_html:
                log_warn(f"[{server_id}] 未找到续期链接，疑似弹窗内容片段: {diag_html}")
            take_screenshot(page, f"renew-link-missing-{server_id}")
            raise RuntimeError("未找到续期链接")

        btn_text = renew_href.get("text", "")
        href = renew_href["href"]

        if btn_text and "renew instance" not in btn_text.lower():
            if not (total_days is not None and total_days <= 7):
                result.update(status="tooearly", emoji="⏳", status_label="冷却期",
                              detail=remaining_before or btn_text)
                return result

        # 执行续期（用 page.goto 直接跳转，不经过 navigate，避免误判 CF）
        log_info(f"[{server_id}] 执行续期跳转: {href}")
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
        try:
            page.wait_for_url(lambda u: "/dashboard" in u or "/server-console" in u, timeout=30000)
        except Exception:
            pass

        url = page.url
        if "success=RENEWED" in url:
            log_info(f"[{server_id}] 续期成功！")
            try:
                navigate(page, server_url)
                time.sleep(5)
                after_text = page.evaluate("""() => {
                    const ids = ['renewal-status-console', 'server-timer-status'];
                    for (const id of ids) {
                        const el = document.getElementById(id);
                        if (el && el.innerText.trim()) return el.innerText.trim();
                    }
                    return null;
                }""")
                log_info(f"[{server_id}] 续期后状态文本: {after_text}")
                result["after"] = parse_remaining(after_text)
                result["after_raw"] = after_text
                log_info(f"[{server_id}] 续期后剩余: {result['after']}")
            except Exception as e:
                log_warn(f"[{server_id}] 读取续期后状态失败: {e}")
            result.update(status="renewed", emoji="✅", status_label="续期成功",
                          detail=f"{result['before'] or '?'} → {result['after'] or '?'}")
        elif "err=CANNOTAFFORDRENEWAL" in url:
            result.update(status="broke", emoji="⚠️", status_label="余额不足",
                          detail=remaining_before or "")
        elif "err=TOOEARLY" in url:
            result.update(status="tooearly", emoji="⏳", status_label="冷却期",
                          detail=remaining_before or "")
        else:
            result.update(status="unknown", emoji="❓", status_label="结果未知")

    except Exception as e:
        log_error(f"[{server_id}] 异常: {e}")
        result.update(status="error", emoji="❌", status_label="脚本异常",
                      detail=str(e)[:80])

    return result


# ── 主流程 ────────────────────────────────────────────────────────────────────
def run():
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少 FREEZEHOST_DISCORD_TOKEN")

    log_info("启动 CloakBrowser（源码级指纹伪装 + Xray 代理）...")

    # headed + humanize + proxy：与 runfc 完全一致
    # geoip=True：根据代理 IP 自动匹配时区/语言，消除指纹矛盾
    browser = launch(
        headless=False,
        humanize=True,
        proxy=PROXY_SERVER,
        geoip=True,
    )
    page = browser.new_page()
    page.set_default_timeout(TIMEOUT)
    log_info("CloakBrowser 就绪")

    display_name = "未知用户"

    try:
        # 出口 IP 验证
        log_info("验证出口 IP（通过代理）...")
        try:
            page.goto("https://api.ipify.org?format=json",
                      wait_until="domcontentloaded", timeout=15000)
            ip = json.loads(page.inner_text("body") or "{}").get("ip", "?")
            log_info(f"出口 IP: {ip}")
        except Exception:
            log_warn("IP 验证超时")

        # 加载首页
        log_info("打开 FreezeHost 登录页")
        if not wait_for_site_ready(page):
            buf = take_screenshot(page, "site-down-final")
            msg = (
                f"用户：{display_name}\n"
                f"🔌 FreezeHost 站点宕机\n"
                f"CONNECTION TO THE MANAGEMENT SERVICES LOST\n"
                f"已重试 {MAX_SITE_RETRIES} 次仍无法连接\n\n"
                f"FreezeHost Auto Renew"
            )
            send_tg(msg, buf)
            log_warn("站点宕机，本次跳过续期")
            return

        # 关闭可能存在的 Cookie/GDPR 同意弹窗（曾遮挡登录按钮导致 confirm-login 一直 hidden）
        dismiss_cookie_consent(page)

        # 点击 Discord 登录
        page.click('span.text-lg:has-text("Login with Discord")', timeout=15_000)

        # 点击后再检测一次（弹窗有时延迟出现/二次出现）
        dismiss_cookie_consent(page, timeout=4)

        confirm_btn = page.locator("button#confirm-login")
        confirm_btn.wait_for(state="visible")
        confirm_btn.click()
        log_info("已接受服务条款")

        page.wait_for_url(re.compile(r"discord\.com"), timeout=15000)
        log_info("已到达 Discord")

        # 注入 Token
        page.evaluate("""(token) => {
            const f = document.createElement('iframe');
            f.style.display = 'none';
            document.body.appendChild(f);
            f.contentWindow.localStorage.setItem('token', '"'+token+'"');
            try { localStorage.setItem('token', '"'+token+'"'); } catch(e) {}
            document.body.removeChild(f);
        }""", DISCORD_TOKEN)
        log_info("Token 已注入")

        page.reload(wait_until="domcontentloaded")
        time.sleep(3)

        if re.search(r"discord\.com/login", page.url):
            take_screenshot(page, "token-failed")
            raise RuntimeError("Token 登录失败")

        log_info("Token 注入成功")

        # OAuth
        try:
            page.wait_for_url(re.compile(r"discord\.com/oauth2/authorize"), timeout=6000)
            time.sleep(2)
            if "discord.com" in page.url:
                handle_oauth_page(page)
            if "discord.com" in page.url:
                try:
                    page.wait_for_url(re.compile(r"free\.freezehost\.pro"), timeout=20000)
                except Exception:
                    take_screenshot(page, "oauth-stuck")
                    raise RuntimeError("OAuth 未跳转")
        except Exception as e:
            if "discord.com" in page.url:
                raise RuntimeError(f"OAuth 超时: {e}")

        # Dashboard
        try:
            page.wait_for_url(lambda u: "/callback" in u or "/dashboard" in u, timeout=10000)
        except Exception:
            pass
        if "/callback" in page.url:
            page.wait_for_url(re.compile(r"/dashboard"), timeout=15000)
        if "/dashboard" not in page.url:
            take_screenshot(page, "not-dashboard")
            raise RuntimeError("未到达 Dashboard")

        log_info("登录成功")

        email = extract_email(page)
        if email:
            display_name = email
        else:
            log_warn("邮箱获取失败，TG 将显示「未知用户」")

        server_ids = discover_server_ids(page)
        if not server_ids:
            buf = take_screenshot(page, "no-servers")
            send_tg(f"用户：{display_name}\n⚠️ 未发现服务器\n\nFreezeHost Auto Renew", buf)
            return

        results, screenshots = [], []
        for sid in server_ids:
            log_info("=" * 50)
            res = process_server(page, sid)
            results.append(res)
            buf = take_screenshot(page, f"server-{_SERVER_INDEX.get(sid, 0)}")
            if buf:
                screenshots.append(buf)

        final_img = (screenshots[0] if len(screenshots) == 1
                     else merge_screenshots(browser, screenshots) if screenshots
                     else None)

        # 写入 renew_result.json 供 workflow 读取
        try:
            all_days = []
            for r in results:
                after_raw   = r.get("after_raw") or ""
                after_days  = remaining_total_days(after_raw)
                before_days = remaining_total_days(r.get("before") or "")
                if r.get("status") == "renewed":
                    if after_days:
                        log_info(f"[{r['server_id']}] 用续期后天数计入统计: {after_days:.2f} 天")
                        all_days.append(after_days)
                    else:
                        log_warn(f"[{r['server_id']}] 续期成功但 after 未读到，跳过该服务器天数统计")
                else:
                    d = after_days or before_days
                    if d:
                        all_days.append(d)
            if all_days:
                with open("renew_result.json", "w") as f:
                    json.dump({"min_remaining_days": min(all_days)}, f)
                log_info(f"写入 renew_result.json: 最小剩余天数 = {min(all_days):.2f}")
        except Exception as ex:
            log_warn(f"写入 renew_result.json 失败: {ex}")

        lines = []
        for r in results:
            s = f"服务器: {r['server_id']} | {r['emoji']}{r['status_label']}"
            if r["detail"]:
                s += f" {r['detail']}"
            lines.append(s)

        send_tg("\n".join([f"用户：{display_name}", *lines, "", "FreezeHost Auto Renew"]), final_img)
        log_info("所有服务器处理完毕")

    except Exception as e:
        buf = take_screenshot(page, "fatal-error")
        send_tg(f"用户：{display_name}\n❌ 异常: {e}\n\nFreezeHost Auto Renew", buf)
        raise
    finally:
        time.sleep(3)
        browser.close()


if __name__ == "__main__":
    try:
        run()
        log_info("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
        sys.exit(1)
