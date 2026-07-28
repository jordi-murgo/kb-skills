#!/usr/bin/env python3
"""M365 Copilot client — query Teams channel messages via WebSocket.

Extracts the Sydney JWT token automatically from a persistent browser session
(Playwright with user data dir), then connects to the Copilot WebSocket to ask
natural language questions about Teams channel messages.

This script covers ONLY Teams channel messages. Emails and 1:1 chats are
handled by scripts/graph-fetch.ps1 (Microsoft Graph API, deterministic).

Usage:
    python3 scripts/m365-copilot.py [optional custom prompt]

If no prompt is given, uses a default prompt that asks for all technical
project context from Teams channel messages in the last 24 hours.

Output:
    - stderr: spinner + progress (can be discarded with 2>/dev/null)
    - stdout: ONLY the final Copilot response (pipeable)

Requirements:
    - Playwright installed (pip install playwright && playwright install chromium)
    - First run: browser opens, you log in to M365, session persists in .browser-session/
    - Subsequent runs: session reused automatically
    - Token auto-extracted from the browser's WebSocket traffic
"""

import asyncio
import base64
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

def find_repo_root() -> Path:
    """Walk up from this file to the vault root (the directory holding wiki/).

    This script lives inside a skill, so its depth below the repo root depends on
    where the skill is installed. Counting .parent levels breaks the moment it
    moves; looking for the marker does not.
    """
    for parent in Path(__file__).resolve().parents:
        # Both markers, not just wiki/: there is a skill directory named "wiki"
        # under .agents/skills/, so the wiki/ marker alone resolves to the skills
        # directory instead of the repo root.
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    raise SystemExit("repo root not found: no ancestor has both wiki/ and .git")


REPO_ROOT = find_repo_root()
BROWSER_DATA_DIR = REPO_ROOT / ".browser-session"
RECORD_SEPARATOR = "\x1e"

def load_project_context() -> tuple[str, str]:
    """Read project name and keywords from kb-config.yaml at the vault root.

    Returns (project_name, keywords_str). Falls back to generic defaults if
    config is missing or the project section is absent — the script still
    works, just with a less targeted prompt.
    """
    candidates = [REPO_ROOT / "kb-config.yaml", REPO_ROOT / "kb-config.yml", REPO_ROOT / "kb-config.json"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return ("the project", "")

    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            import json
            cfg = json.loads(text)
        except json.JSONDecodeError:
            return ("the project", "")
    else:
        try:
            import yaml
            cfg = yaml.safe_load(text)
        except Exception:
            return ("the project", "")

    proj = cfg.get("project", {}) if isinstance(cfg, dict) else {}
    name = proj.get("name", "the project")
    keywords = proj.get("keywords", [])
    keywords_str = ", ".join(keywords) if keywords else ""
    return (name, keywords_str)


def build_default_prompt() -> str:
    """Build a project-aware default prompt from kb-config.yaml."""
    project_name, keywords = load_project_context()
    keyword_line = f" Keywords to watch for: {keywords}.\n" if keywords else ""
    return (
        f"Busca en mensajes de CANALES de Teams (no correos, no chats 1:1) de las ÚLTIMAS 24 HORAS "
        f"todo lo relacionado con {project_name} o cualquier tema técnico del proyecto.\n"
        f"{keyword_line}"
        "Los correos ya los obtengo por otra vía — NO incluyas correos. Solo mensajes de "
        "canales de Teams.\n\n"
        "NO hagas resumen ejecutivo. Lista TODOS los hechos con detalle máximo:\n"
        "- Nombres completos de personas y sus roles/empresas\n"
        "- Nombres exactos de modelos, versiones, endpoints, URLs, namespaces, repos, pods\n"
        "- Comandos, rutas de archivos, nombres de YAMLs, StorageClasses, config keys\n"
        "- Estados de Jira, cambios de sprint, asignaciones\n"
        "- Bloqueos, errores, timeouts, workarounds y quién los reportó/resolvió\n"
        "- Decisiones tomadas Y decisiones pendientes con quién participa\n"
        "- Fechas y horas cuando se mencionen\n"
        "- Nombre del canal de Teams donde se discutió cada tema\n\n"
        "Estructura la respuesta por tema técnico, no por canal. Incluye TODO, "
        "incluso lo que parezca menor — otro agente necesita datos exactos para "
        "actualizar documentación del estado del proyecto."
    )

VARIANTS = (
    "EnableMcpServerWidgets,feature.EnableMcpServerWidgets,"
    "feature.EnableImageGenInsufficientTokensThrottled,"
    "feature.EnableImageGenSystemCapacityThrottled,"
    "feature.EnableLuForChatCIQ,feature.enableChatCIQPlugin,"
    "EnableRequestPlugins,feature.EnableSensitivityLabels,"
    "EnableUnsupportedUrlDetector,feature.IsCustomEngineCopilotEnabled,"
    "feature.bizchatfluxv3,feature.enablechatpages,"
    "feature.turnOnWorkTabRecommendation,feature.turnOnDARecommendation,"
    "feature.IsStreamingModeInChatRequestEnabled,"
    "IncludeSourceAttributionsConcise,SkipPublishEmptyMessage,"
    "feature.EnableDeduplicatingSourceAttributions,"
    "feature.IsCitationsReferencesOutputEnabled,"
    "feature.enableDeltaStreamingForReferences,"
    "feature.enableIncludeReferencesInDeltaResponse,"
    "feature.enablereferencesforagents,Enable3PActionProgressMessages,"
    "feature.enableClientWebRtc,feature.EnableMeetingRecapOfSeriesMeetingWithCiq,"
    "feature.EnableReferencesListCompleteSignal,feature.StorageMessageSplitDisabled,"
    "feature.EnableCuaTakeControlApi,EnableComposeWidget,"
    "feature.EnableMergingPureDeltas,feature.isExternalEmailEnabled,"
    "feature.isExcludedEmailEnabled,feature.disabledisallowedmsgs,"
    "feature.enableCitationsForSynthesisData,feature.EnableConversationShareApis,"
    "feature.enableGenerateGraphicArtOptionsSet,cdximagen,"
    "feature.EnableCuaTakeControlApi,"
    "feature.EnableContentApiandDocTypeHtmlInRichAnswers,"
    "cdxgrounding_api_v2_rich_web_answers_reference_bottom_force,"
    "cdxenablerenderforisocomp,feature.EnableDesignEditorImageGrounding,"
    "feature.EnableDesignerEditor,feature.EnableSkipRehydrationForSpeCIdImages,"
    "feature.sourcescontrolmainline,feature.sourcescontrolmainlineal,"
    "feature.EnableConnectorExecutionControlsAllowlist,"
    "feature.EnableBizchatMainlineExecutionControlsResolution,"
    "feature.EnablePersonalization,cdxentrecapvifluxv3,rich_responses,"
    "feature.EnableBase64DataInMessageAnnotations,"
    "feature.EnableStarterLicenseCheckBypass,feature.DisableMimir3sFlow,"
    "feature.EnablePersonalWorkingSetFor3s,feature.EnableSkipEmittingMessageOnFlush,"
    "feature.EnableRemoveEmptySourceAttributions,"
    "feature.EnableRemoveStreamingMode,feature.OfficeWebToHelix,"
    "feature.OfficeDesktopToHelix,feature.M365TeamsHubToHelix,"
    "feature.OwaHubToHelix,feature.MonarchHubToHelix,"
    "feature.Win32OutlookHubToHelix,feature.MacOutlookHubToHelix,"
    "Agt_bizchat_enableGpt5ForHelix"
)


def log_err(msg: str, end: str = "\n"):
    """Print to stderr only — stdout is reserved for the final response."""
    print(msg, file=sys.stderr, end=end, flush=True)


def spinner_tick(state: list):
    """Advance a simple spinner on stderr."""
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    state[0] = (state[0] + 1) % len(frames)
    log_err(f"\r{frames[state[0]]} ", end="")


def load_env():
    """Load .env.local if present."""
    env_file = REPO_ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def decode_jwt(token: str) -> dict:
    """Decode JWT payload without verification."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


async def extract_token_from_browser() -> dict:
    """Launch Playwright with persistent context, extract Copilot WebSocket token.

    Uses .browser-session/ directory to persist cookies and localStorage across runs.
    First run requires manual login; subsequent runs reuse the session.
    """
    from playwright.async_api import async_playwright

    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Add .browser-session to .gitignore if not already there
    gitignore = REPO_ROOT / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".browser-session" not in content:
            gitignore.write_text(content + "\n# Playwright persistent session\n.browser-session/\n")

    log_err("Launching browser (persistent session)...")
    log_err(f"  Data dir: {BROWSER_DATA_DIR}")

    async with async_playwright() as p:
        # Use persistent context to keep cookies/localStorage across runs
        context = await p.chromium.launch_persistent_context(
            str(BROWSER_DATA_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Install WebSocket interceptor BEFORE navigating — context-level
        # captures WS from any frame/page, including pre-existing ones
        ws_urls = []

        def on_websocket(ws):
            if "substrate.office.com/m365Copilot/Chathub" in ws.url:
                ws_urls.append(ws.url)

        context.on("web_socket", on_websocket)
        page.on("websocket", on_websocket)

        # Navigate to Outlook
        log_err("  Navigating to outlook.cloud.microsoft...")
        await page.goto("https://outlook.cloud.microsoft", wait_until="domcontentloaded", timeout=60000)

        # Check if we need to log in (look for sign-in indicators)
        await page.wait_for_timeout(3000)
        current_url = page.url
        if "login" in current_url or "login.live" in current_url:
            log_err("  Login required. Please sign in to your M365 account.")
            log_err("  Waiting for login to complete (timeout: 120s)...")
            try:
                await page.wait_for_url("**/outlook.cloud.microsoft/**", timeout=120000)
                log_err("  Login successful!")
            except Exception:
                log_err("  ERROR: Login timeout. Please try again.")
                await context.close()
                sys.exit(1)

        # Click Copilot button
        log_err("  Looking for Copilot button...")
        await page.wait_for_timeout(2000)
        try:
            copilot_btn = page.get_by_role("button", name="Copilot")
            if await copilot_btn.count() > 0:
                await copilot_btn.first.click()
                log_err("  Clicked Copilot button")
        except Exception:
            log_err("  Copilot button not found (may already be open)")

        # Wait briefly for WS (Copilot may open it on load), then trigger immediately
        log_err("  Waiting for Copilot WebSocket...")
        for _ in range(3):
            await page.wait_for_timeout(1000)
            if ws_urls:
                break

        if not ws_urls:
            log_err("  No WS yet, triggering by typing in Copilot...")
            try:
                iframe_loc = page.frame_locator('iframe[name*="embedded-page"]')
                textbox = iframe_loc.get_by_role("textbox", name=re.compile("Copilot", re.I))
                await textbox.fill("hello")
                await page.keyboard.press("Enter")
            except Exception as e:
                log_err(f"  Trigger via iframe failed: {e}")
                try:
                    log_err("  Trying direct page typing fallback...")
                    await page.keyboard.type("hello")
                    await page.keyboard.press("Enter")
                except Exception as e2:
                    log_err(f"  Direct typing also failed: {e2}")

            # WS opens shortly after triggering — poll every 500ms
            for _ in range(20):
                await page.wait_for_timeout(500)
                if ws_urls:
                    break

        if not ws_urls:
            log_err("ERROR: Could not capture Copilot WebSocket URL.")
            log_err("Make sure you're logged in to M365 and Copilot is available.")
            await context.close()
            sys.exit(1)

        # Parse the WebSocket URL
        ws_url = ws_urls[0]
        parsed = urlparse(ws_url)
        params = parse_qs(parsed.query)

        access_token = params.get("access_token", [None])[0]
        if not access_token:
            log_err("ERROR: No access_token in WebSocket URL")
            await context.close()
            sys.exit(1)

        # Extract oid and tid from the path
        path_parts = parsed.path.strip("/").split("/")
        oid_at_tid = path_parts[-1] if path_parts else ""
        oid, tid = oid_at_tid.split("@") if "@" in oid_at_tid else ("", "")

        # Check token freshness
        claims = decode_jwt(access_token)
        exp = claims.get("exp", 0)
        now = int(__import__("time").time())
        remaining = exp - now
        if remaining < 60:
            log_err(f"  WARNING: Token expires in {remaining}s — may not be valid long enough.")

        log_err(f"  Token extracted (oid={oid[:8]}..., tid={tid[:8]}..., {remaining//60}min remaining)")

        await context.close()

        return {
            "access_token": access_token,
            "oid": oid,
            "tid": tid,
        }


def build_chat_invocation(prompt: str, session_id: str, conversation_id: str, client_corr_id: str) -> str:
    """Build the SignalR chat invocation message matching the real Copilot UI payload."""
    options_sets = [
        "enterprise_flux_web", "enterprise_flux_work",
        "enable_request_response_interstitials", "enterprise_flux_image_v1",
        "enterprise_toolbox_with_skdsstore_search_message_extensions",
        "enable_ME_auth_interstitial", "enable_confirmation_interstitial",
        "enable_plugin_auth_interstitial", "enable_response_action_processing",
        "enterprise_pagination_support",
        "search_result_progress_messages_with_search_queries",
        "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
        "rich_responses", "gptvnorm2048",
        "enterprise_flux_work_code_interpreter",
        "cwc_code_interpreter_citation_fix", "code_interpreter_interactive_charts",
        "enterprise_code_interpreter_citation_fix",
        "cwc_code_interpreter_interactive_charts_inline_image",
        "code_interpreter_matplotlib_patching", "enable_batch_token_processing",
        "disable_cea_message_listener", "enable_selective_url_redaction",
        "update_memory_plugin", "add_custom_instructions",
        "agent_recommendations", "enable_gg_gpt", "enable_inferred_memory_read",
        "update_textdoc_response_after_streaming",
        "deepleo_networking_timeout_10minutes_canmore",
        "flux_v3_references", "flux_v3_references_entities",
        "flux_v3_image_gen_enable_dimensions",
        "flux_v3_image_gen_enable_non_watermarked_storage",
        "flux_v3_image_gen_enable_icon_dimensions",
        "flux_v3_image_gen_enable_system_text_with_params",
        "flux_v3_image_gen_enable_designer_dimensions_meta_prompting_in_system_prompts",
        "flux_v3_image_gen_enable_story",
    ]

    allowed_message_types = [
        "Chat", "Suggestion", "InternalSearchQuery", "Disengaged",
        "InternalLoaderMessage", "Progress", "GeneratedCode",
        "RenderCardRequest", "AdsQuery", "SemanticSerp",
        "GenerateContentQuery", "GenerateGraphicArt", "SearchQuery",
        "ConfirmationCard", "AuthError", "DeveloperLogs",
        "TriggerPlugin", "HintInvocation", "MemoryUpdate",
        "EndOfRequest", "TriggerConfirmation", "ResumeInvokeAction",
        "ResumeUserInputRequest", "TriggerUserInputRequest", "EscapeHatch",
        "TriggerPluginAuth", "ResumePluginAuth", "ReferencesListComplete",
        "CompleteExtension", "TriggerExtension", "SwitchRespondingEndpoint",
    ]

    invocation = {
        "type": 4,
        "invocationId": "0",
        "target": "chat",
        "arguments": [{
            "source": "owahub",
            "clientCorrelationId": client_corr_id,
            "sessionId": session_id,
            "optionsSets": options_sets,
            "streamingMode": "ConciseWithPadding",
            "options": {},
            "extraExtensionParameters": {},
            "allowedMessageTypes": allowed_message_types,
            "sliceIds": [],
            "threadLevelGptId": {},
            "traceId": client_corr_id,
            "isStartOfSession": True,
            "clientInfo": {
                "clientPlatform": "OwaHub-web",
                "clientAppName": "OwaHub",
                "clientEntrypoint": "owahub",
                "clientSessionId": session_id,
                "clientAppType": "Web",
                "deviceOS": "macOS",
                "deviceType": "Desktop",
                "clientPlatformVersion": "10.15.7",
            },
            "message": {
                "author": "user",
                "inputMethod": "Keyboard",
                "text": prompt,
                "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
                "requestId": client_corr_id,
                "locationInfo": {"timeZoneOffset": 2, "timeZone": "Europe/Madrid"},
                "locale": "ca-es",
                "messageType": "Chat",
                "experienceType": "Default",
                "adaptiveCards": [],
                "clientPreferences": {"executionControls": {"web": {}, "work": {}}},
            },
            "gpts": [{
                "id": "bizchat-as-gpt-scenario",
                "source": "BuiltInAgents",
                "clientOverrides": {
                    "capabilities": [{"name": "WebSearch"}, {"name": "WorkSearch"}],
                    "deepResearchModels@odata.type": "Collection(String)",
                },
            }],
            "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
            "tone": "Magic",
            "renderReferencesBehindEOS": True,
            "disconnectBehavior": "continue",
        }],
    }

    return json.dumps(invocation) + RECORD_SEPARATOR


def build_metrics_frame() -> str:
    """Build the metrics frame sent together with the chat invocation."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    metrics = {
        "type": 1,
        "target": "Metrics",
        "arguments": [{"Timestamps": {"RequestSent": now}, "ReceivedTokenMetrics": {}}],
    }
    return json.dumps(metrics) + RECORD_SEPARATOR


async def query_copilot(prompt: str, token_data: dict) -> str:
    """Connect to Copilot WebSocket and send a query. Returns the full response text."""
    import websockets

    session_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    client_corr_id = str(uuid.uuid4())
    chat_session_id = str(uuid.uuid4())

    base_url = f"wss://substrate.office.com/m365Copilot/Chathub/{token_data['oid']}@{token_data['tid']}"
    params = {
        "chatsessionid": chat_session_id,
        "XRoutingParameterSessionKey": chat_session_id,
        "clientrequestid": client_corr_id,
        "X-SessionId": session_id,
        "ConversationId": conversation_id,
        "access_token": token_data["access_token"],
        "variants": VARIANTS,
        "source": '"owahub"',
        "product": "OwaHub",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": "Starter",
        "isEdu": "false",
        "agent": "work",
        "scenario": "owahub",
    }
    ws_url = base_url + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    log_err("Connecting to Copilot WebSocket...")

    headers = {
        "Origin": "https://outlook.cloud.microsoft",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }

    full_text = ""

    async with websockets.connect(ws_url, additional_headers=headers, max_size=2**20) as ws:
        log_err("  Connected. Handshake...")

        # 1. SignalR handshake
        await ws.send(json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR)
        response = await ws.recv()
        if RECORD_SEPARATOR in response:
            response = response.split(RECORD_SEPARATOR)[0]
        handshake = json.loads(response)
        if handshake.get("error"):
            log_err(f"  Handshake error: {handshake}")
            return ""

        log_err("  Handshake OK. Sending query...")

        # 2. Keepalive
        await ws.send(json.dumps({"type": 6}) + RECORD_SEPARATOR)

        # 3. Chat invocation + metrics
        chat_msg = build_chat_invocation(prompt, session_id, conversation_id, client_corr_id)
        metrics_msg = build_metrics_frame()
        await ws.send(chat_msg + metrics_msg)

        log_err(f"  Query sent. Waiting for response...")
        spinner_state = [0]

        # 4. Receive streaming response
        buffer = ""
        last_msg_time = asyncio.get_event_loop().time()
        DEBUG_WS = os.environ.get("COPILOT_DEBUG", "")
        done = False

        try:
            while not done:
                try:
                    # 30s silence timeout — if no data for 30s, something is wrong
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    last_msg_time = asyncio.get_event_loop().time()
                    buffer += raw

                    while RECORD_SEPARATOR in buffer:
                        frame, buffer = buffer.split(RECORD_SEPARATOR, 1)
                        if not frame:
                            continue

                        msg = json.loads(frame)
                        msg_type = msg.get("type")

                        if DEBUG_WS:
                            target = msg.get("target", "")
                            n_args = len(msg.get("arguments", []))
                            log_err(f"  [debug] type={msg_type} target={target} args={n_args}")

                        if msg_type == 1:  # StreamItem
                            spinner_tick(spinner_state)

                            args = msg.get("arguments", [{}])
                            if args:
                                messages = args[0].get("messages", [])
                                for m in messages:
                                    content_origin = m.get("contentOrigin", "")

                                    if DEBUG_WS:
                                        log_err(f"  [debug]   origin={content_origin} text_len={len(m.get('text', ''))}")

                                    if content_origin == "DeepLeo":
                                        if m.get("adaptiveCards"):
                                            card_text = m["adaptiveCards"][0].get("body", [{}])[0].get("text", "")
                                            if card_text and card_text != full_text:
                                                full_text = card_text
                                        elif m.get("text", ""):
                                            full_text = m["text"]

                                    # Handle writeAtCursor incremental updates
                                    write_at_cursor = args[0].get("writeAtCursor")
                                    if write_at_cursor and content_origin != "DeepLeo":
                                        full_text += write_at_cursor

                        elif msg_type == 2:  # Completion — final response
                            item = msg.get("item", {})
                            if DEBUG_WS:
                                log_err(f"  [debug] completion item keys={list(item.keys())}")
                            for m in item.get("messages", []):
                                if m.get("author") == "bot":
                                    text = m.get("text", "")
                                    if m.get("adaptiveCards"):
                                        card_text = m["adaptiveCards"][0].get("body", [{}])[0].get("text", "")
                                        full_text = card_text or text
                                    else:
                                        full_text = text
                            done = True

                        elif msg_type == 3:  # Close invocation
                            if DEBUG_WS:
                                log_err(f"  [debug] close invocation: {msg.get('error', 'no error')}")
                            done = True

                        elif msg_type == 6:  # Ping
                            await ws.send(json.dumps({"type": 6}) + RECORD_SEPARATOR)

                        elif msg_type == 7:  # Close
                            done = True

                except asyncio.TimeoutError:
                    elapsed = asyncio.get_event_loop().time() - last_msg_time
                    log_err(f"\n  Timeout — no data for {elapsed:.0f}s.")
                    break

        except Exception as e:
            if "1000" not in str(e) and "OK" not in str(e):
                log_err(f"\n  Error: {e}")

    log_err("")  # newline after spinner
    return full_text


async def main():
    load_env()

    prompt = sys.argv[1] if len(sys.argv) > 1 else build_default_prompt()

    # Step 1: Extract token from browser
    token_data = await extract_token_from_browser()

    # Step 2: Query Copilot
    response = await query_copilot(prompt, token_data)

    # Step 3: Print ONLY the final response to stdout
    if response:
        # Clean up citation markers like 【1-129f1d】
        clean = re.sub(r"【\d+-[a-f0-9]+】", "", response)
        # Clean up unicode citation markers
        clean = re.sub(r"\uE200.*?\uE201", "", clean)
        clean = re.sub(r"\uE202.*?\uE202", "", clean)
        clean = clean.strip()
        print(clean)

        # Step 4: Save timestamped copy to .raw/msoffice/
        raw_dir = REPO_ROOT / ".raw" / "msoffice"
        raw_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_file = raw_dir / f"copilot-resume-{ts}.md"
        raw_file.write_text(clean, encoding="utf-8")
        log_err(f"  Saved to {raw_file.relative_to(REPO_ROOT)}")
    else:
        log_err("No response received.")


if __name__ == "__main__":
    asyncio.run(main())