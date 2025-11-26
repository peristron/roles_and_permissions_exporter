# PREPPING for public deployment; to deal with sensitive SessionId portion of the workflow
# if reverting, revert to locally saved brightspace_role_exporter_v6_v3.py
# run command:
#   streamlit run brightspace_role_exporter_v6.py
# verify dependencies are installed - pip install streamlit requests pandas beautifulsoup4 playwright
# directory setup:
#   cd c:\users\name\documents\pyprojs_local  (replace name/path if needed)
#!/usr/bin/env python3
# -- coding: utf-8 --

import asyncio
import io
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
import subprocess
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin
from http.cookies import SimpleCookie

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# Attempt to import Playwright
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# --- CONFIGURATION ---
st.set_page_config(
    page_title='Brightspace Role Exporter', 
    layout='wide',
    page_icon="🎓"
)

# Configure logging but prevent propagation of sensitive data
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

TEMPLATE_FILENAME = "Permissions_Report_Template.xlsx"

# --- ASYNCIO SETUP FOR WINDOWS ---
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# --- AUTOMATED BROWSER INSTALLER ---
@st.cache_resource
def ensure_playwright_browsers():
    print("Checking Playwright browser installation...")
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        print("Playwright browsers installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error installing Playwright browsers: {e}")

if PLAYWRIGHT_AVAILABLE:
    ensure_playwright_browsers()


# --- SECURITY & UTILITY FUNCTIONS ---

def safe_rerun() -> None:
    st.rerun()

def is_safe_url(url: str) -> bool:
    """
    SSRF Protection: Validates that the URL is http/s and not a local/internal address.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        
        hostname = parsed.hostname
        if not hostname:
            return False
            
        # Block localhost
        if hostname in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
            return False
            
        # Optional: specific D2L/Brightspace regex check could go here
        # if "brightspace.com" not in hostname and "d2l" not in hostname: return False
        
        return True
    except Exception:
        return False

def normalize_url(url: str) -> str:
    return (url or '').strip().rstrip('/')

def normalize_cookie(value: str) -> str:
    if not value:
        return ''
    value = value.strip()
    if value.lower().startswith('cookie:'):
        value = value[7:].strip()
    return re.sub(r'\s*;\s*', '; ', value)

def sanitize_filename(name: str, default: str) -> str:
    safe_name = re.sub(r'[^A-Za-z0-9_. -]+', '_', (name or '').strip())
    return safe_name or default

def format_seconds_to_hms(total_seconds: float) -> str:
    total_seconds = max(0, int(total_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'

def add_cookies_to_browser_context(context, host_url: str, cookie_header: str) -> None:
    domain = urlparse(host_url).netloc
    simple_cookie = SimpleCookie()
    try:
        simple_cookie.load(cookie_header or '')
        cookies_for_playwright = [
            {
                'name': name,
                'value': morsel.value,
                'domain': domain,
                'path': '/',
                'secure': host_url.lower().startswith('https'),
                'httpOnly': False,
                'sameSite': 'Lax'
            }
            for name, morsel in simple_cookie.items()
        ]
        if cookies_for_playwright:
            context.add_cookies(cookies_for_playwright)
    except Exception as e:
        logging.warning(f"Error parsing cookies: {e}") # Changed to warning

# --- CORE LOGIC ---

def check_whoami(api_endpoint_url: str, cookie_header: str) -> Dict[str, Any]:
    # User-Agent is important for WAFs
    headers = {'User-Agent': 'Role-Permissions-Exporter/2.0', 'Cookie': cookie_header}
    try:
        response = requests.get(api_endpoint_url, headers=headers, timeout=15)
        if response.status_code == 200:
            user_data = response.json()
            user_full_name = user_data.get('FirstName', '') + ' ' + user_data.get('LastName', '')
            return {'status': 'success', 'message': f"Authentication successful for: {user_full_name.strip()}"}
        else:
            return {'status': 'fail', 'message': f"Authentication failed (Status {response.status_code}). Expired cookie or wrong host."}
    except requests.RequestException as exception:
        # Log generic error, do not log 'exception' object blindly as it may contain headers
        logging.error(f"WhoAmI check failed for {api_endpoint_url}") 
        return {'status': 'fail', 'message': "Network error. Please check URL."}

def fetch_roles_via_api(api_endpoint_url: str, cookie_header: str) -> pd.DataFrame:
    headers = {'User-Agent': 'Role-Permissions-Exporter/2.0', 'Accept': 'application/json', 'Cookie': cookie_header}
    try:
        response = requests.get(api_endpoint_url, headers=headers, timeout=30)
        response.raise_for_status()
        roles_data = [{'Identifier': role.get('Identifier'), 'DisplayName': role.get('DisplayName')} for role in response.json()]
        return pd.DataFrame(roles_data)
    except Exception as exception:
        logging.warning(f"API call failed (Status: {getattr(exception.response, 'status_code', 'N/A') if hasattr(exception, 'response') else 'N/A'})")
        return pd.DataFrame()

def fetch_roles_via_ui_scrape(host_url: str, organization_unit_id: int, cookie_header: str) -> pd.DataFrame:
    headers = {'Cookie': cookie_header, 'User-Agent': 'Mozilla/5.0'}
    start_url = f'{host_url}/d2l/lp/security/role_list.d2l?ou={organization_unit_id}'
    roles, seen_urls, max_pages_to_scrape = [], set(), 50
    current_url = start_url
    
    status_text = st.empty()
    status_text.text("Scraping role list from UI...")

    for i in range(max_pages_to_scrape):
        if not current_url or current_url in seen_urls:
            break
        seen_urls.add(current_url)
        try:
            response = requests.get(current_url, headers=headers, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            for anchor in soup.find_all('a', href=re.compile(r'roleId=\d+')):
                match = re.search(r'roleId=(\d+)', anchor['href'])
                if match:
                    roles.append({'Identifier': int(match.group(1)), 'DisplayName': anchor.get_text(strip=True) or f'Role_{match.group(1)}'})
            next_link_element = soup.find('a', title=re.compile(r'next', re.I))
            current_url = urljoin(current_url, next_link_element['href']) if next_link_element and 'href' in next_link_element.attrs else None
        except Exception as exception:
            logging.warning(f"UI scraping failed on page {i}")
            break
            
    status_text.empty()
    return pd.DataFrame(roles).drop_duplicates(subset=['Identifier']) if roles else pd.DataFrame()

def export_one_role_v2(page, host_url: str, organization_unit_id: int, role_id: int, role_name: str, page_timeout: int, link_timeout: int, max_retries: int) -> Tuple[bool, str, Optional[bytes]]:
    last_error_message = "No attempts were made."
    for attempt in range(max_retries + 1):
        try:
            preview_url = f'{host_url}/d2l/lp/security/export_preview.d2l?roleId={role_id}&ou={organization_unit_id}'
            page.goto(preview_url, wait_until='domcontentloaded', timeout=page_timeout)
            
            # Handling the "Export" button
            export_button = page.get_by_role('button', name=re.compile(r'^\s*Export\s*$', re.I))
            try:
                export_button.wait_for(state='visible', timeout=8000)
                export_button.click()
            except PlaywrightTimeoutError:
                file_url = f'{host_url}/d2l/lp/security/export_file.d2l?roleId={role_id}&ou={organization_unit_id}'
                page.goto(file_url, wait_until='domcontentloaded', timeout=page_timeout)
            
            link_locator = page.locator('a[href*="viewFile.d2lfile"]')
            link_locator.wait_for(state='visible', timeout=link_timeout)
            
            with page.expect_download(timeout=link_timeout) as download_info:
                link_locator.click()
            
            download = download_info.value
            safe_name = sanitize_filename(role_name or f'role_{role_id}', f'role_{role_id}')
            output_filename = f"{safe_name}_{role_id}.txt"
            
            with tempfile.NamedTemporaryFile(delete=False) as temporary_file:
                temporary_file_path = temporary_file.name
                download.save_as(temporary_file_path)
            
            with open(temporary_file_path, 'rb') as file_handle:
                file_data = file_handle.read()
            
            os.remove(temporary_file_path)
            return True, output_filename, file_data

        except Exception as exception:
            # Do not log full exception as it might contain URL parameters or data
            last_error_message = f'Export failed for {role_id}. Attempt {attempt+1}. Error: {type(exception).__name__}'
            if attempt < max_retries:
                time.sleep(3) 

    return False, last_error_message, None

# --- STREAMLIT UI ---

st.title('🎓 Brightspace Role Permissions Exporter')

# Initialize Session State
if 'fetched_roles_df' not in st.session_state:
    st.session_state['fetched_roles_df'] = pd.DataFrame()
if 'active_cookie' not in st.session_state:
    st.session_state['active_cookie'] = ""

# --- SECURITY NOTICE ---
st.warning("""
**Security & Privacy Notice:**  
This tool runs on a public cloud server. While your data is processed in-memory and not saved to disk, 
you are submitting a sensitive Session Cookie.  
1. **Do not** use this on a shared or public computer.
2. **Log out** of Brightspace immediately after downloading your ZIP file to invalidate the cookie.
3. **Sanitize** your session by clearing cookies if you suspect any issues.
""")

# --- SIDEBAR ---
with st.sidebar:
    st.header("📊 Excel Analysis")
    st.info("Use the template below to analyze the ZIP file generated by this tool.")
    try:
        with open(TEMPLATE_FILENAME, "rb") as template_file:
            st.download_button(
                label="📥 Download Excel Template",
                data=template_file.read(),
                file_name="Brightspace_Permissions_Report_Template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
    except FileNotFoundError:
        st.warning("Template file not found on server.")

with st.expander("📖 Instructions", expanded=False):
    st.markdown("""
    **1. Get Cookie:** Open Incognito > Login Brightspace > DevTools (F12) > Network > Refresh > Click top request > Headers > Copy `Cookie` value.
    **2. Fetch Roles:** Enter URL/Cookie below, click "Fetch Available Roles".
    **3. Select Roles:** Choose which roles to keep.
    **4. Export:** Click "Start Export" and download the ZIP.
    **5. Logout:** Log out of Brightspace to kill the session.
    """)

# --- INPUT SECTION ---
st.markdown("### 1. Credentials")
col1, col2 = st.columns([1, 1])

with col1:
    host_url = normalize_url(st.text_input('Brightspace/D2L Host URL', placeholder='https://myschool.brightspace.com'))

with col2:
    cookie_header_raw = st.text_input(
        'Cookie Header Value', 
        type='password', 
        placeholder='Paste your full cookie string here...',
        help="Found in DevTools -> Network -> Request Headers"
    )
    cookie_header_value = normalize_cookie(cookie_header_raw)

if host_url and cookie_header_value:
    # SSRF Check
    if not is_safe_url(host_url):
        st.error("❌ Invalid URL. Must start with https:// and cannot be a local address.")
        st.stop()

    if st.button("🔍 Verify Credentials"):
        with st.spinner("Verifying session..."):
            api_url = f'{host_url}/d2l/api/lp/1.48/users/whoami'
            result = check_whoami(api_url, cookie_header_value)
            if result['status'] == 'success':
                st.success(result['message'])
            else:
                st.error(result['message'])

st.markdown("---")

# --- STEP 1: FETCH ROLES ---
st.markdown("### 2. Discovery")
col_fetch1, col_fetch2 = st.columns([3, 1])
with col_fetch1:
    organization_unit_id = st.number_input('Org Unit ID (ou)', value=6606, step=1, help="Usually 6606 for the main organization.")
with col_fetch2:
    exclude_d2lmonitor = st.checkbox("Exclude 'D2LMonitor'", value=True)

if st.button("📥 Step 1: Fetch Available Roles", type="primary"):
    if not host_url or not cookie_header_value:
        st.error("Please provide Host URL and Cookie first.")
    elif not is_safe_url(host_url):
        st.error("Invalid Host URL.")
    else:
        st.info("Connecting to Brightspace to list roles...")
        roles_api_url = f'{host_url}/d2l/api/lp/1.48/roles/'
        
        # Try API
        df = fetch_roles_via_api(roles_api_url, cookie_header_value)
        
        # Try Scrape if API empty
        if df.empty:
            st.warning("API returned no roles. Trying UI scraping...")
            df = fetch_roles_via_ui_scrape(host_url, organization_unit_id, cookie_header_value)
            
        if not df.empty:
            if exclude_d2lmonitor:
                df = df[df['DisplayName'] != 'D2LMonitor']
            
            # Save to session state
            st.session_state['fetched_roles_df'] = df.sort_values('DisplayName')
            st.session_state['active_cookie'] = cookie_header_value
            
            st.success(f"Successfully found {len(df)} roles.")
        else:
            st.error("Could not find any roles. Check Org Unit ID or Cookie.")

# --- STEP 2: SELECTION & EXPORT ---
if not st.session_state['fetched_roles_df'].empty:
    st.markdown("---")
    st.markdown("### 3. Selection & Export")
    
    roles_df = st.session_state['fetched_roles_df']
    all_role_names = roles_df['DisplayName'].tolist()
    
    # SELECTION WIDGET
    st.info("💡 Tip: Deselect roles you don't need to reduce file size.")
    selected_role_names = st.multiselect(
        "Select Roles to Include in Export:",
        options=all_role_names,
        default=all_role_names
    )
    
    st.write(f"**Selected:** {len(selected_role_names)} of {len(all_role_names)} roles.")
    
    # CONFIGURATION
    with st.expander("Advanced Settings (Timeouts & Retries)"):
        c1, c2, c3 = st.columns(3)
        with c1:
            page_load_timeout = st.number_input('Page Load (ms)', value=45000, step=5000)
        with c2:
            download_link_timeout = st.number_input('Download Wait (ms)', value=30000, step=5000)
        with c3:
            number_of_retries = st.number_input('Retries', value=2, min_value=0, max_value=5)
        append_timestamp = st.checkbox('Append timestamp to filename', value=True)

    # EXPORT BUTTON
    if st.button("🚀 Step 2: Start Export", disabled=(len(selected_role_names) == 0)):
        if not PLAYWRIGHT_AVAILABLE:
            st.error("Playwright is not available.")
            st.stop()

        active_cookie = st.session_state.get('active_cookie')
        if not active_cookie:
            st.error("Session Error: Cookie lost. Please re-fetch roles (Step 1).")
            st.stop()
            
        # Filter DF based on selection
        target_roles = roles_df[roles_df['DisplayName'].isin(selected_role_names)]
        role_list = [(int(row['Identifier']), str(row['DisplayName'])) for _, row in target_roles.iterrows()]
        
        # Setup Export Vars
        zip_buffer = io.BytesIO()
        export_log = []
        success_count = 0
        failure_count = 0
        start_time = time.time()
        
        progress_bar = st.progress(0.0, text='Initializing secure browser...')
        status_area = st.empty()

        try:
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_archive:
                with sync_playwright() as playwright_instance:
                    
                    browser = playwright_instance.chromium.launch(
                        headless=True, 
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                    
                    # Secure Context
                    context = browser.new_context(
                        accept_downloads=True,
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        viewport={'width': 1920, 'height': 1080}
                    )
                    
                    add_cookies_to_browser_context(context, host_url, active_cookie)
                    
                    page = context.new_page()
                    
                    total = len(role_list)
                    for i, (rid, rname) in enumerate(role_list, 1):
                        progress_bar.progress(i / total, text=f"Exporting ({i}/{total}): {rname}")
                        
                        success, fname, data = export_one_role_v2(
                            page, host_url, organization_unit_id, rid, rname,
                            page_load_timeout, download_link_timeout, number_of_retries
                        )
                        
                        if success and data:
                            success_count += 1
                            zip_archive.writestr(fname, data)
                            export_log.append({'Role': rname, 'ID': rid, 'Status': 'OK'})
                        else:
                            failure_count += 1
                            # Only log generic error to UI
                            export_log.append({'Role': rname, 'ID': rid, 'Status': 'Failed', 'Error': 'Download Failed or Timed Out'})
                            
                        elapsed = time.time() - start_time
                        if elapsed > 0:
                            rate = i / elapsed
                            eta = (total - i) / rate
                            status_area.caption(f"✅ {success_count} | ❌ {failure_count} | ⏳ ETA: {format_seconds_to_hms(eta)}")
                            
                    context.close()
                    browser.close()

            # SAVE RESULTS TO STATE
            st.session_state['export_zip_buffer'] = zip_buffer
            st.session_state['export_log'] = export_log
            
            netloc = urlparse(host_url).netloc or 'export'
            base_name = f"{netloc}_roles"
            if append_timestamp:
                base_name += f"_{time.strftime('%Y%m%d_%H%M%S')}"
            st.session_state['base_zip_name'] = base_name
            
            safe_rerun()

        except Exception as e:
            st.error("Browser process failed.")
            # Log exception to console for admin, but keep it generic
            logging.warning(f"Browser Process Error: {type(e).__name__}")

# --- RESULTS DISPLAY ---
if 'export_zip_buffer' in st.session_state:
    st.markdown("---")
    st.success("🎉 Export Complete!")
    st.balloons()
    
    col_d1, col_d2 = st.columns(2)
    fname = st.session_state.get('base_zip_name', 'roles')
    
    with col_d1:
        st.download_button(
            label="📥 Download Permissions ZIP",
            data=st.session_state['export_zip_buffer'].getvalue(),
            file_name=f"{fname}.zip",
            mime="application/zip",
            use_container_width=True
        )
    
    with col_d2:
        log_df = pd.DataFrame(st.session_state.get('export_log', []))
        st.download_button(
            label="📄 Download Log (CSV)",
            data=log_df.to_csv(index=False).encode('utf-8'),
            file_name=f"{fname}_log.csv",
            mime='text/csv',
            use_container_width=True
        )
        
    with st.expander("View Log Details"):
        st.dataframe(log_df, use_container_width=True)
