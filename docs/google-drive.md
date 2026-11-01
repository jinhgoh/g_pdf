# Google Drive access

Gpdf uses Google's hosted picker to browse PDFs in your system browser. The app asks for the `https://www.googleapis.com/auth/drive.file` scope, which limits access to files authorized for this app instead of granting access to every file in your Drive. Previously authorized files can remain authorized in Google until you revoke the app's access. See [Google's scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) and [desktop picker documentation](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker).

## Click to connect

Click **Google Drive** in the toolbar (or **Gpdf > File > Open from Google Drive…**). A configured build opens Google's sign-in and file picker directly in your default browser, using that browser profile's existing Google session when available. Google may still require account selection, consent, or reauthentication. Set your preferred signed-in browser as the Windows default browser. Gpdf does not read or copy browser cookies, and Google handles your password.

There is no client-JSON prompt for users. A build without app registration displays a configuration message and makes no authorization request. Registration is a one-time developer step, distinct from each user's Google login.

## Developer setup (once for the app)

1. Create or select a project in Google Cloud Console. Enable the **Google Drive API** and **Google Picker API**.
2. Configure the Google Auth platform branding, audience, and data access. Request only the `drive.file` scope. When the app is in testing, add the Google accounts that will test it to the test-user list. Organization policy can restrict which apps users may authorize.
3. Create an OAuth client with application type **Desktop app** and download its JSON configuration. Do not use a web client, service account, or an API key in its place.
4. For development, place the downloaded configuration at `gpdf/google_oauth_client.json` (ignored by Git). Gpdf loads only this fixed package resource; it does not search the current working directory or environment for credentials.
5. To ship a configured executable, run `./build.ps1 -InstallDependencies -GoogleOAuthClient "C:\path\to\client.json"`. The build validates and embeds only the Desktop client ID and client secret under the fixed resource name. If the argument is omitted, the build uses `gpdf/google_oauth_client.json` when present. No user tokens are bundled. Existing executables are not updated by editing source files.
6. Launch the configured app and click **Google Drive**. Sign in in your system browser if needed and select a PDF. The picker grants access to the chosen file and returns to Gpdf.

Gpdf uses the installed-app authorization-code flow with PKCE and an ephemeral loopback callback. The callback accepts connections on `127.0.0.1`, not other network interfaces. Fixed Google endpoints are used instead of endpoint URLs supplied in the JSON. A desktop OAuth client identifier is public application configuration; its bundled client secret cannot be treated as a confidential server credential. Use a project intended for this application and audience, and keep its local configuration out of source control. Never bundle a service-account private key, web-client secret, or user access/refresh token. See [Google's desktop OAuth documentation](https://developers.google.com/identity/protocols/oauth2/native-app).

Public distribution may require Google consent-screen review, accurate privacy disclosures, and compliance with Google's user-data policies. Configure these for the actual product and audience before release; creating a client ID does not constitute Google approval.

## Open, edit, save, and disconnect

- Browse and choose PDFs in the Google-hosted picker. Gpdf downloads the selected PDF before opening it. Network work runs separately from the GUI; once downloaded, the existing page renderer and bounded cache handle reading.
- **Save edited copy to Drive / Ctrl+S** writes the local working PDF and uploads a **new file in the root of My Drive**. Every save creates a separate edited copy. The original file is never overwritten or deleted. The new copy does not inherit the source's folder or sharing permissions. There is no background synchronization or upload on each edit.
- **Save as / Ctrl+Shift+S** writes a local copy and detaches that document from its Drive destination. Later saves affect the local copy.
- An upload failure keeps local edits available. If a connection fails after Google accepted the file but before Gpdf received confirmation, inspect My Drive before retrying; another save can create another copy. Gpdf does not merge concurrent PDF edits or update the original file.
- **Disconnect Google Drive** clears in-memory credentials and requests token revocation. If network revocation fails, remove the app in your Google account's third-party connections. Closing the app clears the session's credentials but does not itself revoke Google's prior file grants.

If the owner disables downloading or copying, the app must respect that restriction. Google Docs conversions, arbitrary Drive URLs, updating existing Drive files, and downloading files outside the granted access are not part of this feature.

## Data handling and limits

Gpdf's Drive integration sends authentication requests to Google and transfers the selected PDF directly between your computer and Google Drive. It does not add an application-operated proxy, analytics service, or document-processing server. The browser and Google services apply their own privacy policies. Saving creates a new file without copying source permissions or requesting sharing changes. Verify the copy's visibility in Drive when organization policies or account defaults affect sharing.

Access and refresh tokens are kept only in process memory. They are not written to configuration, logs, or a token cache by this integration. Sign-in must be repeated in a new app session. OAuth uses a random state value and PKCE to resist forged callbacks and stolen authorization codes. HTTPS certificate validation stays enabled; automatic redirects and environment-provided proxy settings are disabled for API requests to reduce credential forwarding risks. Networks that require an explicit proxy may therefore be unsupported.

Downloaded PDFs and edited working copies are ordinary local files in an app-created temporary directory under the user's local application-data folder. Windows permissions are restricted before document bytes are written. Remote names are display labels, not local filesystem paths. Normal cleanup removes temporary files when they are no longer used and on shutdown. Cleanup is not secure erasure: crashes, forced termination, operating-system paging, backups, antivirus software, or forensic recovery may retain data. Use a protected Windows account and encrypted storage for sensitive documents; do not treat this app as protection against malware running as your user or an administrator.

Downloads are streamed with a size limit and PDF-header checks. These checks reject some invalid inputs but are not malware scanning. PDF parsing runs in the existing worker process; this is a reliability boundary, **not an operating-system security sandbox**. Keep Python, Qt, MuPDF, Requests, and the browser updated. A malicious PDF or compromised dependency remains a security risk.

Remote saves deliberately use file creation, with no update or delete request against an existing file. This prevents Gpdf from overwriting someone else's concurrent changes. Supporting in-place updates would require a separately verified server-side concurrency mechanism; a metadata version check alone leaves a race between checking and uploading. See [Drive files.create](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/create).

## Validation before release

Automated tests use generated PDFs and mocked network responses; they do not establish that a live Google account, project configuration, or every server-side condition works. Use a disposable test account and documents for these manual checks:

1. Sign in, cancel sign-in, choose a PDF, and confirm that unselected files are not accessible to the app.
2. Edit and save a PDF, then independently download the new copy and verify its annotations, original content, encryption, and access permissions. Confirm that the original file was unchanged and that saving again creates a separate copy.
3. Edit the source file in another client after opening it in Gpdf. Confirm that saving in Gpdf creates a new file and leaves the other client's version untouched.
4. Interrupt a transfer, expire or revoke credentials, and deny download/edit permission. Confirm that errors preserve local edits and do not expose tokens or response bodies.
5. Save a local copy, disconnect, close normally, and force-close in a separate test. Inspect temporary-file cleanup and document the crash-residual behavior.
6. Open representative large scans and run the repository benchmarks. Confirm that browser sign-in and transfers do not block scrolling in an already open document.

No security audit or penetration test is implied by these checks. Do not claim zero data leakage or immunity from hacking.
