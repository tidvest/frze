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
    关闭 GDPR/广告联盟 Cookie 同意弹窗。
    本站使用 Google Funding Choices (FC)，按钮文案会按访问者地区自动翻译
    （荷兰语/中文/英文等），所以优先用 FC 库固定不变的 CSS class 匹配，
    不随语言变化；翻译文案匹配仅作兜底。点击用 force=True，防止被
    某层透明遮罩/动画判定为不可点击而一直报错被吞掉。
    """
    # Google Funding Choices 的"同意"主按钮，class 名固定，不随语言/地区变化
    stable_selectors = [
        "button.fc-cta-consent",
        "button.fc-primary-button",
        ".fc-consent-root button.fc-cta-consent",
        # 其他常见 CMP 的固定选择器，兜底
        "#onetrust-accept-btn-handler",
        ".qc-cmp2-summary-buttons button[mode='primary']",
    ]
    consent_texts = [
        "Toestemming geven", "Akkoord", "Accepteren",
        "Accept all", "Accept", "I Agree", "I agree", "Agree",
        "同意", "接受", "Allow all", "Allow",
    ]

    def try_click(locator) -> bool:
        try:
            if not locator.is_visible(timeout=300):
                return False
        except Exception:
            return False
        for force in (False, True):
            try:
                locator.click(timeout=1500, force=force)
                return True
            except Exception as e:
                log_warn(f"  点击同意按钮失败(force={force}): {e}")
                continue
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in stable_selectors:
            try:
                btn = page.locator(sel).first
                if try_click(btn):
                    log_info(f"已关闭 Cookie 同意弹窗（选择器: {sel}）")
                    time.sleep(0.5)
                    return True
            except Exception:
                continue
        for txt in consent_texts:
            try:
                btn = page.locator(f'button:has-text("{txt}")').first
                if try_click(btn):
                    log_info(f"已关闭 Cookie 同意弹窗（按钮文案: {txt}）")
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


def fetch_internal_id(page, server_id: str) -> str | None:
    """
    通过 /api/serverdetails 拿 internal_id。
    这个站的续费按钮(#action-link-modal) href 属性永远是 "#"，
    真正的跳转地址是 loadServerDetails() 里动态绑定到 .onclick 的：
        renewLinkModal.onclick = () => location.href = `../renew?id=${internal_id}`
    读 href 属性 / 正则扫整页 HTML 在这个站上根本读不到任何东西（DOM 里
    压根不存在 "renew?id=..." 这种字面字符串），所以改成直接调用页面
    自己也在用的这个接口拿 internal_id，自己拼 URL，最稳。
    """
    try:
        data = page.evaluate(f"""async () => {{
            try {{
                const r = await fetch('/api/serverdetails?id={server_id}');
                if (!r.ok) return null;
                const j = await r.json();
                return (j && j.attributes) ? j.attributes.internal_id : null;
            }} catch (e) {{ return null; }}
        }}""")
        return str(data) if data else None
    except Exception as e:
        log_warn(f"[{server_id}] 获取 internal_id 异常: {e}")
        return None


# ── CF Turnstile（续期页新增的 "Security Verification" 勾选框，逻辑移植自 zampto_auto.py） ──
_cf_frame_seen_ts = {"seen": False, "first_check_ts": None}


def _reset_cf_frame_seen():
    _cf_frame_seen_ts["seen"] = False
    _cf_frame_seen_ts["first_check_ts"] = None


def turnstile_state(page, debug: bool = False) -> str:
    # 以 challenges.cloudflare.com iframe 是否存在为主要依据，
    # 比 DOM 结构更可靠
    cf_iframe_exists = any(
        "challenges.cloudflare.com" in (f.url or "") for f in page.frames
    )
    if cf_iframe_exists:
        if not _cf_frame_seen_ts["seen"]:
            _cf_frame_seen_ts["seen"] = True
        token_ready = page.evaluate("""() => {
            function deepQuery(root, sel) {
                let el = root.querySelector(sel);
                if (el) return el;
                for (const host of root.querySelectorAll('*')) {
                    if (host.shadowRoot) {
                        el = deepQuery(host.shadowRoot, sel);
                        if (el) return el;
                    }
                }
                return null;
            }
            var tokenEl = deepQuery(document, 'input[name="cf-turnstile-response"]');
            return !!(tokenEl && (tokenEl.value || '').length > 10);
        }""")
        if token_ready:
            if debug:
                log_info("[诊断/turnstile_state] cf_iframe_exists=True, token_ready=True → done")
            return 'done'
        if debug:
            log_info("[诊断/turnstile_state] cf_iframe_exists=True, token_ready=False → unchecked")
        return 'unchecked'

    # iframe 不存在，检查验证卡片是否还在
    # （FreezeHost 续期页样式："Security Verification" / "verify you are human"）
    modal_state = page.evaluate("""() => {
        var bodyTxt = document.body ? (document.body.innerText || '') : '';
        if (bodyTxt.includes('Security Verification') ||
            bodyTxt.includes('verify you are human') ||
            bodyTxt.includes('geen robot') ||
            bodyTxt.includes('Mensch')) return 'modal_open';
        return 'no_modal';
    }""")

    if modal_state != 'modal_open':
        if debug:
            log_info(f"[诊断/turnstile_state] no cf_iframe, modal_state={modal_state} → done")
        return 'done'

    if _cf_frame_seen_ts["first_check_ts"] is None:
        _cf_frame_seen_ts["first_check_ts"] = time.time()

    if _cf_frame_seen_ts["seen"]:
        if debug:
            log_info("[诊断/turnstile_state] frame 曾出现过现已消失 → done")
        return 'done'

    elapsed = time.time() - _cf_frame_seen_ts["first_check_ts"]
    if elapsed < 2.5:
        if debug:
            log_info(f"[诊断/turnstile_state] 等待 iframe 加载，elapsed={elapsed:.1f}s → verifying")
        return 'verifying'

    if debug:
        log_info(f"[诊断/turnstile_state] 宽限期已过({elapsed:.1f}s)仍未见 iframe，验证卡片仍存在 → unchecked（强制点击）")
    return 'unchecked'


def click_turnstile_checkbox(page, timeout=10) -> bool:
    def dump_frames(label: str):
        try:
            frames = page.frames
            log_info(f"[诊断/{label}] 当前共 {len(frames)} 个 frame：")
            for i, f in enumerate(frames):
                url = (f.url or "about:blank")[:120]
                log_info(f"  [{i}] {url}")
        except Exception as e:
            log_warn(f"[诊断/{label}] dump_frames 失败: {e}")

    cf_frame = None
    for _ in range(timeout * 2):
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame:
            break
        time.sleep(0.5)

    box = None
    if cf_frame:
        log_info(f"找到 Turnstile frame: {cf_frame.url[:120]}")
        time.sleep(1)
        try:
            box = cf_frame.frame_element().bounding_box()
            log_info(f"[诊断] frame bounding_box={box}")
        except Exception as e:
            log_warn(f"获取 Turnstile frame bounding_box 失败: {e}")
    else:
        log_warn("枚举 frames 未找到 Turnstile frame")
        dump_frames("frame未找到")

    if not box:
        try:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
            log_info(f"[诊断] 降级 iframe bounding_box={box}")
        except Exception as e:
            log_warn(f"降级定位 Turnstile iframe 失败: {e}")

    if not box:
        log_warn("未能定位 Turnstile checkbox，跳过点击")
        dump_frames("定位失败")
        return False

    if not (0 < box["x"] < VIEWPORT_W and 0 < box["y"] < VIEWPORT_H):
        log_warn(f"[诊断] bounding_box 坐标异常（{box}），跳过点击")
        return False

    x = box["x"] + 25
    y = box["y"] + box["height"] / 2
    try:
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.2, 0.4))
        page.mouse.click(x, y)
        log_info(f"✅ 已点击 Turnstile checkbox ({x:.0f}, {y:.0f})")
        return True
    except Exception as e:
        log_warn(f"点击 Turnstile checkbox 失败: {e}")
        return False


def wait_cf_turnstile(page, timeout=40) -> bool:
    log_info("等待 Cloudflare Turnstile 验证...")
    _reset_cf_frame_seen()

    # ── 验证卡片/Turnstile 存在性检测（最多等 8s） ──────────────────
    turnstile_or_modal_visible = False
    for _retry in range(16):  # 16 × 0.5s = 8s
        cf_iframe_exists = any(
            "challenges.cloudflare.com" in (f.url or "") for f in page.frames
        )
        if cf_iframe_exists:
            log_info("【Turnstile】检测到 challenges.cloudflare.com iframe，进入处理流程")
            turnstile_or_modal_visible = True
            break

        dom_visible = page.evaluate("""() => {
            var bodyTxt = document.body ? (document.body.innerText || '') : '';
            return bodyTxt.includes('Security Verification') ||
                   bodyTxt.includes('verify you are human') ||
                   bodyTxt.includes('geen robot') ||
                   bodyTxt.includes('Mensch');
        }""")
        if dom_visible:
            log_info(f"【Turnstile】检测到验证卡片（DOM 结构，第 {_retry + 1} 次）")
            turnstile_or_modal_visible = True
            break

        time.sleep(0.5)

    if not turnstile_or_modal_visible:
        log_warn("⚠️ 8s 内未检测到 Turnstile iframe 或验证卡片（可能未触发验证或已静默通过）")
        return True

    start = time.time()
    deadline = start + timeout

    # ── 阶段1：静默等待自动通过 ──────────────────────────────────
    log_info("【Turnstile】阶段1：静默等待自动通过（最多 20s，稳定 unchecked 后提前进入点击）...")
    silent_deadline = min(time.time() + 20, deadline)
    last_state = None
    stable_unchecked_count = 0
    while time.time() < silent_deadline:
        last_state = turnstile_state(page, debug=True)
        if last_state == "done":
            log_info("✅ CF Turnstile 静默通过")
            return True
        if last_state == "unchecked":
            stable_unchecked_count += 1
            if stable_unchecked_count >= 3:
                log_info("【Turnstile】连续观察到稳定 unchecked，提前结束静默等待")
                break
        else:
            stable_unchecked_count = 0
        time.sleep(0.5)

    if last_state == "verifying":
        log_info("【Turnstile】阶段1.5：仍在验证中（转圈），额外等待最多 12s...")
        grace_deadline = min(time.time() + 12, deadline)
        while time.time() < grace_deadline:
            state = turnstile_state(page)
            if state == "done":
                log_info("✅ CF Turnstile 静默通过（宽限期内）")
                return True
            if state == "unchecked":
                log_info("【Turnstile】宽限期内 spinner 结束，转为未勾选状态")
                break
            time.sleep(0.5)

    # ── 阶段2：主动点击，最多 3 次 ─────────────────────────────
    log_info("【Turnstile】阶段2：未自动通过，主动点击勾选框...")
    for attempt in range(1, 4):
        if time.time() >= deadline:
            break
        state = turnstile_state(page)
        if state == "done":
            return True
        if state == "verifying":
            wait_until = min(time.time() + 5, deadline)
            while time.time() < wait_until and turnstile_state(page) == "verifying":
                time.sleep(0.5)
            if turnstile_state(page) == "done":
                return True

        take_screenshot(page, f"cf_before_click_{attempt}")
        clicked = click_turnstile_checkbox(page, timeout=min(8, max(1, int(deadline - time.time()))))
        take_screenshot(page, f"cf_after_click_{attempt}")

        if not clicked:
            log_warn(f"第 {attempt} 次点击 Turnstile checkbox 失败")
            time.sleep(1)
            continue

        click_wait_deadline = min(time.time() + 8, deadline)
        while time.time() < click_wait_deadline:
            if turnstile_state(page) == "done":
                log_info(f"✅ CF Turnstile 验证完成（第 {attempt} 次点击后）")
                return True
            time.sleep(0.5)

        log_warn(f"第 {attempt} 次点击后仍未验证通过，{'重试...' if attempt < 3 else '放弃重试'}")

    # ── 阶段3：剩余时间继续被动等待 ──────────────────────────────
    log_info("【Turnstile】阶段3：继续等待剩余时间...")
    while time.time() < deadline:
        if turnstile_state(page) == "done":
            log_info("✅ CF Turnstile 验证完成")
            return True
        elapsed = int(time.time() - start)
        if elapsed % 5 == 0:
            log_info(f"  CF 等待中... {elapsed}s")
        time.sleep(1)

    log_error(f"CF Turnstile 验证超时（{timeout}s）")
    take_screenshot(page, "cf_timeout")
    return False


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

        # 获取续期目标地址：不读 href（这个站上读不到），直接拿 internal_id 自己拼
        internal_id = fetch_internal_id(page, server_id)
        if not internal_id:
            take_screenshot(page, f"renew-id-missing-{server_id}")
            raise RuntimeError("未能获取 internal_id，无法构造续期链接")

        href = f"../renew?id={internal_id}"
        log_info(f"[{server_id}] internal_id={internal_id}")

        # 执行续期（用 page.goto 直接跳转，不经过 navigate，避免误判 CF）
        log_info(f"[{server_id}] 执行续期跳转: {href}")
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
        take_screenshot(page, f"renew-page-{server_id}")

        # 续期页新增了 Cloudflare Turnstile 人机验证（"Security Verification" 勾选框），
        # 需要主动检测并点击，否则会一直卡在验证页导致续期失败
        log_info(f"[{server_id}] 检测续期页人机验证...")
        nav_detected = False
        try:
            turnstile_ok = wait_cf_turnstile(page, timeout=40)
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ("context", "destroyed", "navigation", "detached")):
                nav_detected = True
                turnstile_ok = True
                log_info(f"[{server_id}] ✅ 检测到页面自动重载（验证通过后自动续期完成）: {e}")
            else:
                log_warn(f"[{server_id}] wait_cf_turnstile 异常: {e}")
                turnstile_ok = False

        if not (turnstile_ok or nav_detected):
            log_warn(f"[{server_id}] ⚠️ 人机验证未确认通过，继续尝试读取跳转结果...")

        take_screenshot(page, f"renew-after-cf-{server_id}")

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
        dismiss_cookie_consent(page, timeout=5)

        # 点击 Discord 登录
        page.click('span.text-lg:has-text("Login with Discord")', timeout=15_000)

        # Google FC 同意弹窗是异步脚本注入的，经常在页面"稳定"之后才延迟出现，
        # 所以不能只检测一次就不管了：循环等待 confirm-login 出现，期间持续尝试关闭弹窗
        confirm_btn = page.locator("button#confirm-login")
        login_modal_deadline = time.time() + 60
        while time.time() < login_modal_deadline:
            if confirm_btn.is_visible(timeout=500):
                break
            dismiss_cookie_consent(page, timeout=2)
            time.sleep(0.5)
        else:
            take_screenshot(page, "confirm-login-timeout")
            raise RuntimeError("等待 confirm-login 按钮超时（可能被同意弹窗持续遮挡）")

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
