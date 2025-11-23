# PREPPING for public deployment; to deal with sensitive SessionId portion of the workflow
# run command:
#   streamlit run brightspace_role_exporter_v5.py
# verify dependencies are installed - pip install streamlit requests pandas beautifulsoup4 playwright   THEN   python -m playwright install (shouldn't need to do this)
# directory setup:
#   cd c:\users\oakhtar\documents\pyprojs_local  (replace name/path if needed)
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
st.set_page_config(page_title='Brightspace Role Exporter', layout='wide')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Name of the Excel template file in your repository (MUST INCLUDE .xlsx EXTENSION)
TEMPLATE_FILENAME = "experimental_Dummy_RoleNames_TEMPLATE_roles_and_permissions_report_12312040.xlsx"

# --- ASYNCIO SETUP FOR WINDOWS ---
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# --- UTILITY FUNCTIONS ---

def safe_rerun() -> None:
    st.rerun()

def normalize_url(url: str) -> str:
    return (url or '').strip().rstrip('/')

def normalize_cookie(value: str) -> str:
    if not value:
        return ''
    value = value.strip()
    # Remove "Cookie:" prefix if user pasted the whole header line
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
        logging.error(f"Error parsing cookies: {e}")

# --- CORE LOGIC (Caching REMOVED for security on public server) ---
# NOTE: @st.cache_data is intentionally removed from functions taking 'cookie_header'
# to prevent session tokens from being stored in the server's cache system.

def check_whoami(api_endpoint_url: str, cookie_header: str) -> Dict[str, Any]:
    """Checks validity of the cookie without caching the result."""
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
        return {'status': 'fail', 'message': f"Network error: {exception}"}

def fetch_roles_via_api(api_endpoint_url: str, cookie_header: str) -> pd.DataFrame:
    headers = {'User-Agent': 'Role-Permissions-Exporter/2.0', 'Accept': 'application/json', 'Cookie': cookie_header}
    try:
        response = requests.get(api_endpoint_url, headers=headers, timeout=30)
        response.raise_for_status()
        roles_data = [{'Identifier': role.get('Identifier'), 'DisplayName': role.get('DisplayName')} for role in response.json()]
        return pd.DataFrame(roles_data)
    except Exception as exception:
        logging.error(f"API call to fetch roles failed: {exception}")
        return pd.DataFrame()

def fetch_roles_via_ui_scrape(host_url: str, organization_unit_id: int, cookie_header: str) -> pd.DataFrame:
    headers = {'Cookie': cookie_header, 'User-Agent': 'Mozilla/5.0'}
    start_url = f'{host_url}/d2l/lp/security/role_list.d2l?ou={organization_unit_id}'
    roles, seen_urls, max_pages_to_scrape = [], set(), 50
    current_url = start_url
    
    # Simple progress indicator since we removed cache spinner
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
            logging.error(f"UI scraping for roles failed on page {current_url}: {exception}")
            break
            
    status_text.empty()
    return pd.DataFrame(roles).drop_duplicates(subset=['Identifier']) if roles else pd.DataFrame()

def export_one_role_v2(page, host_url: str, organization_unit_id: int, role_id: int, role_name: str, page_timeout: int, link_timeout: int, max_retries: int) -> Tuple[bool, str, Optional[bytes]]:
    last_error_message = "No attempts were made."
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                logging.info(f"Retrying role '{role_name}' (ID: {role_id}), attempt {attempt + 1}/{max_retries + 1}...")

            preview_url = f'{host_url}/d2l/lp/security/export_preview.d2l?roleId={role_id}&ou={organization_unit_id}'
            page.goto(preview_url, wait_until='domcontentloaded', timeout=page_timeout)
            
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
            last_error_message = f'Failed to export role {role_id} ({role_name}) on attempt {attempt + 1}: {exception}'
            logging.warning(last_error_message)
            if attempt < max_retries:
                time.sleep(3) 

    return False, last_error_message, None

# --- STREAMLIT UI ---

st.title('Brightspace Role Permissions Exporter')

# --- SIDEBAR: PHASE 2 TEMPLATE DOWNLOAD ---
def render_analysis_sidebar():
    with st.sidebar:
        st.header("📊 Phase 2: Analysis - IF you want to proceed with the fuller 'Roles/Permissions Report' ")
        st.info("Once you have the ZIP file from the main window, use this Excel template to generate your report.")
        
        try:
            # We assume the file is in the same directory as this script
            with open(TEMPLATE_FILENAME, "rb") as template_file:
                template_byte = template_file.read()
            
            # NOTE: We read the long filename from disk, but offer it as a cleaner name for download
            st.download_button(
                label="📥 Download Excel Template",
                data=template_byte,
                file_name="Brightspace_Permissions_Report_Template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
            
            with st.expander("📝 How to use the Template", expanded=False):
                st.markdown("""
                **Setup:**
                1.  **Unzip** the Role Permissions file you downloaded.
                2.  Open the **Excel Template**.
                
                **Generate Report:**
                1.  Go to the **Instructions** sheet (or the first sheet).
                2.  Paste the **full folder path** of your unzipped files into the input cell (usually **B3**).
                3.  Go to the **Data** tab in the ribbon.
                4.  Click **Refresh All**.
                """)
                
        except FileNotFoundError:
            st.warning(f"⚠️ Template file not found.\n({TEMPLATE_FILENAME})")
            st.caption("Please ensure the Excel file is uploaded to the repository root and matches the filename in the code.")

render_analysis_sidebar()

# --- MAIN HELP GUIDE ---
def render_help_guide():
    with st.expander("📖 How to use this tool (Step-by-Step Guide)", expanded=False):
        st.markdown("""
        ### 1. Preparation (Security First!)
        *   Open a **New Incognito/Private Window** in your browser.
        *   Log into your Brightspace instance as an Administrator.
        *   *Reason:* This ensures that when you close the window later, the session is destroyed immediately.

        ### 2. Getting the Cookie (The Tricky Part)
        You need the full `Cookie` header string so this tool can "impersonate" your session to download the files.
        
        **Chrome / Edge Instructions:**
        1.  While on your Brightspace homepage, right-click anywhere and select **Inspect** (or press `F12`).
        2.  Click on the **Network** tab in the panel that opens.
        3.  **Refresh** the page (F5). You will see a list of files appear.
        4.  Scroll to the very top of the list and click the first item (usually named `home` or `d2l`).
        5.  On the right side, click the **Headers** tab.
        6.  Scroll down to the section named **Request Headers**.
        7.  Find the line that starts with **Cookie:**.
        8.  Right-click the value (the long string of text) -> **Copy value**.
        
        ### 3. Running the Export
        1.  Paste your **Host URL** (e.g., `https://univ.brightspace.com`).
        2.  Paste the copied text into the **Cookie Header Value** box below.
        3.  Click **Verify Credentials** to ensure it works.
        4.  Click **Start Export**.

        ### 4. Cleanup
        *   Download your ZIP file.
        *   **Log out** of Brightspace.
        *   Close your Incognito window.
        """)

# --- CALL THE HELP GUIDE FUNCTION ---
render_help_guide()

# SECURITY WARNING BLOCK
with st.expander("🔒 Security & Privacy Notice", expanded=True):
    st.markdown("""
    **Important:** This application requires your Brightspace **Session Cookie**. 
    
    1.  **Your Data:** The cookie is used only in memory to authenticate the export request. It is **not** saved to a database or permanent storage.
    2.  **Volatility:** Once you close this tab or refresh the page, the cookie data is lost from the application memory.
    3.  **Best Practice:** Use an Incognito/Private window. After you download your ZIP file, **Log Out** of Brightspace to invalidate the session cookie you used here.
    """)

st.markdown("---")

col1, col2 = st.columns([1, 1])

with col1:
    host_url = normalize_url(st.text_input('Brightspace/D2L Host URL', placeholder='https://myschool.brightspace.com'))

with col2:
    # SECURITY FIX: Changed to type='password' to mask input on screen
    cookie_header_raw = st.text_input(
        'Cookie Header Value', 
        type='password', 
        placeholder='Paste your full cookie string here...',
        help="This field is masked for your privacy. Paste the value from your browser's DevTools (Network Tab -> any request -> request headers -> Cookie)."
    )
    cookie_header_value = normalize_cookie(cookie_header_raw)

# AUTHENTICATION CHECK
if host_url and cookie_header_value:
    if st.button("🔍 Verify Credentials"):
        with st.spinner("Verifying session..."):
            api_url = f'{host_url}/d2l/api/lp/1.48/users/whoami'
            result = check_whoami(api_url, cookie_header_value)
            if result['status'] == 'success':
                st.success(result['message'])
            else:
                st.error(result['message'])

st.markdown("---")

if PLAYWRIGHT_AVAILABLE:
    with st.form('export_form'):
        st.subheader("Export Configuration")
        
        c1, c2 = st.columns(2)
        with c1:
            organization_unit_id = st.number_input('Org Unit ID (ou)', value=6606, step=1, help="Usually 6606 for the main organization.")
            exclude_d2lmonitor_role = st.checkbox("Exclude 'D2LMonitor' role", value=True)
        with c2:
            append_timestamp_to_filename = st.checkbox('Append timestamp to filename', value=True)
            run_headless = st.checkbox("Run Headless", value=True, disabled=True, help="Must run headless on cloud servers.")

        with st.expander("Advanced Timeout Settings"):
            page_load_timeout = st.number_input('Page Load Timeout (ms)', value=45000, min_value=10000, step=1000)
            download_link_timeout = st.number_input('Download Wait Timeout (ms)', value=30000, min_value=10000, step=1000)
            number_of_retries = st.number_input('Retries on Failure', min_value=0, max_value=5, value=2)

        st.markdown("<br>", unsafe_allow_html=True)
        submit_button = st.form_submit_button('🚀 Start Export', type="primary")

    if submit_button:
        if not host_url or not cookie_header_value:
            st.error("Please provide both Host URL and Cookie Header.")
            st.stop()

        # Clear previous session state data
        for key in ['export_zip_buffer', 'export_log', 'success_count']:
            if key in st.session_state:
                del st.session_state[key]
        
        st.info("Fetching role list...")
        roles_api_url = f'{host_url}/d2l/api/lp/1.48/roles/'
        
        # Try API first
        roles_dataframe = fetch_roles_via_api(roles_api_url, cookie_header_value)
        
        # Fallback to UI scraping
        if roles_dataframe.empty:
            st.warning("API role discovery returned no data. Attempting UI scraping...")
            roles_dataframe = fetch_roles_via_ui_scrape(host_url, organization_unit_id, cookie_header_value)
        
        if roles_dataframe.empty:
            st.error("Fatal: Could not discover any roles. Please check your Cookie permissions or Org Unit ID.")
            st.stop()
        
        if exclude_d2lmonitor_role:
            roles_dataframe = roles_dataframe[roles_dataframe['DisplayName'] != 'D2LMonitor']
        
        st.success(f"Ready to process {len(roles_dataframe)} roles.")
        
        # Init processing vars
        role_list = [(int(row['Identifier']), str(row['DisplayName'])) for _, row in roles_dataframe.iterrows()]
        progress_bar = st.progress(0.0, text='Initializing secure browser environment...')
        status_area = st.empty()
        
        zip_buffer = io.BytesIO()
        export_log = []
        success_count = 0
        failure_count = 0
        start_time = time.time()

        # Playwright Execution
        try:
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_archive:
                with sync_playwright() as playwright_instance:
                    # NOTE: Cloud environments usually require specific args for Chromium
                    browser = playwright_instance.chromium.launch(
                        headless=True, 
                        args=['--no-sandbox', '--disable-dev-shm-usage'] # Added for stability in containers
                    )
                    browser_context = browser.new_context(accept_downloads=True)
                    add_cookies_to_browser_context(browser_context, host_url, cookie_header_value)
                    page = browser_context.new_page()
                    
                    total_roles = len(role_list)

                    for index, (role_id, role_name) in enumerate(role_list, 1):
                        progress_bar.progress(index / total_roles, text=f"Exporting: {role_name}")
                        
                        success, filename_or_message, file_data = export_one_role_v2(
                            page, host_url, organization_unit_id, role_id, role_name, 
                            page_load_timeout, download_link_timeout, number_of_retries
                        )
                        
                        if success and file_data:
                            success_count += 1
                            zip_archive.writestr(filename_or_message, file_data)
                            export_log.append({'RoleID': role_id, 'RoleName': role_name, 'Status': 'Success', 'Details': filename_or_message})
                        else:
                            failure_count += 1
                            export_log.append({'RoleID': role_id, 'RoleName': role_name, 'Status': 'Failed', 'Details': filename_or_message})
                        
                        elapsed = time.time() - start_time
                        rate = index / elapsed if elapsed > 0 else 0
                        eta = (total_roles - index) / rate if rate > 0 else 0
                        status_area.caption(f"✅ {success_count} | ❌ {failure_count} | ⏳ ETA: {format_seconds_to_hms(eta)}")

                    page.close()
                    browser_context.close()
                    browser.close()
            
            # Store results
            st.session_state['export_zip_buffer'] = zip_buffer
            st.session_state['export_log'] = export_log
            st.session_state['success_count'] = success_count
            
            netloc = urlparse(host_url).netloc or 'export'
            base_zip_name = f"{netloc}_ou{organization_unit_id}_permissions"
            if append_timestamp_to_filename:
                base_zip_name += f"_{time.strftime('%Y%m%d_%H%M%S')}"
            st.session_state['base_zip_name'] = base_zip_name
            
            safe_rerun()

        except Exception as e:
            st.error(f"An error occurred during the browser session: {e}")
            logging.error(e)

if 'export_zip_buffer' in st.session_state:
    st.balloons()
    st.success("Export Complete!")
    
    base_zip_name = st.session_state.get('base_zip_name', 'role_permissions')
    
    col_dl1, col_dl2 = st.columns(2)
    
    with col_dl1:
        st.download_button(
            label="📥 Download Permissions ZIP",
            data=st.session_state['export_zip_buffer'].getvalue(),
            file_name=f"{base_zip_name}.zip",
            mime="application/zip",
            use_container_width=True
        )
    
    with col_dl2:
        log_df = pd.DataFrame(st.session_state.get('export_log', []))
        st.download_button(
            label="📄 Download Log (CSV)",
            data=log_df.to_csv(index=False).encode('utf-8'),
            file_name=f"{base_zip_name}_log.csv",
            mime='text/csv',
            use_container_width=True
        )
    
    with st.expander("View Execution Log"):
        st.dataframe(log_df, use_container_width=True)

else:
    if not PLAYWRIGHT_AVAILABLE:
        st.error("⚠️ Playwright not found. If on Streamlit Cloud, ensure 'packages.txt' contains 'chromium'.")




