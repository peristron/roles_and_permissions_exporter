# Brightspace Role Permissions Exporter

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Streamlit](https://img.shields.io/badge/Streamlit-App-ff4b4b)
![Playwright](https://img.shields.io/badge/Playwright-Automation-green)

A simplified tool for D2L Brightspace administrators to bulk export permissions for all User Roles in an instance.

Brightspace does not provide a native "Export All" button for role permissions; administrators usually have to click into every single role manually to export the settings. This tool automates that process using a headless browser, compressing all results into a single ZIP file.

## 🚀 Live Demo
*[Insert your Streamlit Cloud URL here once deployed]*

## ✨ Features

*   **Bulk Extraction:** Iterates through every role in the Org Unit and downloads the permission text file.
*   **Secure by Design:** Runs entirely in temporary memory. No cookies or data are saved to a database or disk.
*   **Smart Detection:** Attempts to use the D2L API for role discovery first, falling back to UI scraping if necessary.
*   **Resilient:** Includes retry logic for network hiccups and timeouts.
*   **MFA Compatible:** Uses an existing Session Cookie, bypassing the need to automate 2FA/SSO login flows.

## 🔒 Security & Privacy

This tool requires a **Session Cookie** to authenticate with Brightspace.
*   **Memory Only:** The cookie is used strictly to authenticate the automation session in RAM. It is wiped immediately after execution or page refresh.
*   **Masked Input:** The UI masks the cookie input field to prevent over-the-shoulder snooping.
*   **Recommendation:** Always use this tool in an Incognito/Private window and **Log Out** of Brightspace immediately after downloading your ZIP file to invalidate the session.

## 📖 How to Use

### Option A: Run on Streamlit Cloud (Web)
1.  Open the app link.
2.  Enter your Brightspace **Host URL** (e.g., `https://univ.brightspace.com`).
3.  Paste your **Session Cookie** (see instructions below).
4.  Click **Verify Credentials** to test connection.
5.  Click **Start Export**.

### Option B: Run Locally
If you prefer to run this on your own machine for maximum security:

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
    cd YOUR_REPO_NAME
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    playwright install chromium
    ```

3.  **Run the app:**
    ```bash
    streamlit run brightspace_role_exporter_v3.py
    ```

## 🍪 How to get your Session Cookie

To allow the script to download files on your behalf, you need to grab your session ID from your browser.

1.  Open a **New Incognito Window** and log into Brightspace as Admin.
2.  Right-click the page and select **Inspect** (or press `F12`).
3.  Go to the **Network** tab.
4.  Refresh the page.
5.  Click the first request in the list (usually named `home` or `d2l`).
6.  Look at the **Request Headers** section on the right.
7.  Copy the entire value of the **Cookie:** field.

## 🛠️ Deployment Requirements

If deploying to Streamlit Cloud, ensure you have these two files in your repository:

**requirements.txt**
```text
streamlit
pandas
requests
beautifulsoup4
playwright
