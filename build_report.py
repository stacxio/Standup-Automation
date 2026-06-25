"""Build the standup report from the fetched Slack data and push it to Google Sheets.

Reads the structured rows below (one per real standup post, with the
reflective columns inferred/summarized from each message), de-duplicates them,
writes a Sheets-importable CSV, and upserts the data into a Google Spreadsheet
via a service account (the SRS push stage). Re-running fully replaces the tab's
contents, so it is idempotent — no duplicate rows.

Config (env / .env), matching .env.example:
  GOOGLE_SA_KEY_PATH   path to the service-account JSON (default credentials/service-account.json)
  SPREADSHEET_ID       id of the target Google Sheet (share it with the service-account email as Editor)
  SHEET_TAB_NAME       worksheet/tab name (default "Standup")

Run:  .venv/Scripts/python.exe build_report.py
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from dotenv import load_dotenv

HEADERS = [
    "Developer Name",
    "Date",
    "Projectname",
    "Task",
    "Value",
    "What got moved?",
    "Why it matters?",
    "Blockers?",
    "What's Next?",
    "Status",
]

# Derived work status per (Developer, Date), sourced from the #stacx-check-in
# Slack channel (id C0B9FD2KB5L) as of 2026-06-22. Rule applied per dev+date:
#   - blockers reported in the check-in        -> "Blocked"
#   - evening update present and no blockers    -> "Completed"
#   - morning check-in only (no evening)        -> "In Progress"
# Rows with no matching check-in fall back to "No check-in" (the channel only
# has data from 2026-06-10 onward).
NO_CHECKIN = "No check-in"
STATUS_BY_KEY = {
    ("G N", "2026-06-10"): "Completed",
    ("G N", "2026-06-11"): "Blocked",
    ("G N", "2026-06-12"): "In Progress",
    ("G N", "2026-06-15"): "Blocked",
    ("G N", "2026-06-16"): "Completed",
    ("G N", "2026-06-17"): "In Progress",
    ("G N", "2026-06-18"): "Blocked",
    ("G N", "2026-06-19"): "Blocked",
    ("G N", "2026-06-22"): "In Progress",
    ("Jegan", "2026-06-10"): "Blocked",
    ("Jegan", "2026-06-11"): "In Progress",
    ("Raghul", "2026-06-10"): "Blocked",
    ("Raghul", "2026-06-11"): "Blocked",
    ("Raghul", "2026-06-12"): "In Progress",
    ("Raghul", "2026-06-15"): "Blocked",
    ("Raghul", "2026-06-16"): "In Progress",
    ("Raghul", "2026-06-17"): "Blocked",
    ("Raghul", "2026-06-18"): "Completed",
    ("Raghul", "2026-06-19"): "In Progress",
    ("Raghul", "2026-06-22"): "In Progress",
    ("Sahil Thakur", "2026-06-10"): "Completed",
    ("Sahil Thakur", "2026-06-11"): "Completed",
    ("Sahil Thakur", "2026-06-12"): "Completed",
    ("Sahil Thakur", "2026-06-15"): "Completed",
    ("Sahil Thakur", "2026-06-16"): "Completed",
    ("Sahil Thakur", "2026-06-17"): "In Progress",
    ("Sahil Thakur", "2026-06-18"): "Completed",
    ("Sahil Thakur", "2026-06-19"): "Completed",
    ("Sahil Thakur", "2026-06-20"): "In Progress",
    ("Soma Pani", "2026-06-11"): "Completed",
    ("Soma Pani", "2026-06-12"): "Completed",
    ("Soma Pani", "2026-06-16"): "In Progress",
    ("Soma Pani", "2026-06-18"): "In Progress",
    ("Soma Pani", "2026-06-22"): "In Progress",
}

# Background colours for the Status column (applied as Google Sheets
# conditional-format rules for readability).
STATUS_FILL = {
    "Completed": "C6EFCE",
    "In Progress": "FFEB9C",
    "Blocked": "FFC7CE",
    NO_CHECKIN: "E7E6E6",
}

# Each row is in HEADERS order. Sourced from the live Slack standup channel
# (fetch stage), one row per substantive standup post. Pure social/coordination
# chatter (greetings, @-mentions, "who's joining when") is excluded.
ROWS = [
    # ---------------- G N — Vfarm IoT platform / Vision POC ----------------
    ["G N", "2026-05-11", "Vfarm (IoT Dashboard)", "Build device dashboard search bar, device data filter, and online/offline status", "Faster device lookup and at-a-glance fleet health", "Started core device-management UI features", "Operators must quickly find devices and see which are live", "None reported", "Wire the features to live device data"],
    ["G N", "2026-05-11", "Vfarm (IoT Dashboard)", "Search bar, env-driven Vite dev proxy, device data filter, online/offline status, Settings → Users page + flow", "Working device-management UI plus configurable dev env and user admin", "Completed day's dashboard features and added Users settings page", "Establishes core dashboard usability and environment config", "None reported", "Integrate chat UI; set up mail"],
    ["G N", "2026-05-12", "Vfarm (Ask Genie chat)", "Integrate chat UI with Bharag (agent backend); set up mail", "Brings a conversational assistant into the product", "Began chat UI integration", "Enables in-app AI assistance (Ask Genie)", "None reported", "Finish chat UI and farm APIs"],
    ["G N", "2026-05-12", "Vfarm (Ask Genie chat)", "Complete Ask Genie chat UI; build individual farm APIs", "Working chat assistant and per-farm data access", "Ask Genie chat UI done; farm APIs in progress", "Chat assistant ready; farm-level APIs unlock farm data", "Individual farm APIs still in progress", "Finish individual farm APIs; add API encryption"],
    ["G N", "2026-05-13", "Vfarm (API Security)", "Add API encryption with expiry", "Secures API access with time-limited tokens", "Started API encryption/expiry work", "Protects API endpoints and limits token lifetime", "None reported", "Build token API and UI"],
    ["G N", "2026-05-13", "Vfarm (API Security)", "Create token API, build token UI, and wire them together", "End-to-end token issuance flow", "Token API and UI built and wired together", "Users can generate and manage API tokens", "None reported", "Fix token bugs"],
    ["G N", "2026-05-14", "Vfarm (API Security)", "Fix API token bugs", "Reliable token flow", "Began token bug fixing", "Stabilizes the new token feature", "None reported", "Complete fix and run E2E"],
    ["G N", "2026-05-14", "Vfarm (API Security / Admin)", "Fix API token bug; complete E2E; build Super Admin UI", "Verified token flow end-to-end plus admin controls", "Token bug fixed, E2E done, Super Admin UI started", "Token feature stable; admin UI enables management", "Super Admin UI in progress", "Finish Super Admin UI; write documentation"],
    ["G N", "2026-05-15", "Vfarm (Documentation)", "Write full platform documentation", "Knowledge capture for the platform", "Started full documentation", "Onboarding and maintainability", "None reported", "Continue documentation"],
    ["G N", "2026-05-15", "Vfarm (Documentation)", "Continue documentation (19 sections done)", "Progressing documentation coverage", "19 documentation sections completed", "Builds platform knowledge base", "Documentation not yet finished", "Finish remaining documentation"],
    ["G N", "2026-05-18", "Vfarm (SDK / Security)", "Implement SDK key rotation", "Stronger key security via rotation", "Started SDK key rotation", "Reduces risk from leaked keys", "None reported", "Fix mobile UI"],
    ["G N", "2026-05-19", "Vfarm (Dashboard UI)", "Fix mobile UI", "Usable on mobile devices", "Started mobile UI fixes", "Responsive access for field users", "None reported", "Add MQTT support"],
    ["G N", "2026-05-20", "Vfarm (IoT Connectivity)", "Add MQTT support", "Real-time device messaging protocol", "Began MQTT support", "MQTT is the standard for IoT device comms", "None reported", "Implement MQTT support"],
    ["G N", "2026-05-21", "Vfarm (IoT Connectivity)", "Implement MQTT support", "Devices communicate via MQTT", "MQTT implementation underway", "Enables real-time device telemetry and commands", "None reported", "Plan the Vision POC"],
    ["G N", "2026-05-22", "Vfarm (Vision POC)", "Plan the Vision POC", "Roadmap for computer-vision plant monitoring", "Vision POC planning started", "De-risks the vision feature before build", "None reported", "Begin Vision POC implementation"],
    ["G N", "2026-06-01", "Vfarm (Observability)", "Set up OpenTelemetry logs / observability platform", "Centralized telemetry for monitoring", "Started OTEL observability setup", "Needed to monitor system health", "None reported", "Set up New Relic dashboard"],
    ["G N", "2026-06-02", "Vfarm (Observability)", "Full New Relic dashboard setup", "Live monitoring dashboards", "New Relic dashboard set up", "Visibility into system metrics", "None reported", "Test HTTP in Vision POC"],
    ["G N", "2026-06-03", "Vfarm (Vision POC)", "Test HTTP in Vision POC; connect camera to Raspberry Pi and send image over HTTP", "Proves the image capture/transport pipeline", "Camera-to-Pi image transfer over HTTP working in test", "Foundation for on-device plant vision", "None reported", "Implement LLM with Ollama"],
    ["G N", "2026-06-04", "Vfarm (Vision POC / AI)", "Implement LLM using Ollama", "Local LLM inference for vision analysis", "Started Ollama LLM implementation", "On-prem inference avoids cloud cost and latency", "None reported", "Improve plant-health analysis model"],
    ["G N", "2026-06-05", "Vfarm (Vision POC / AI)", "Improve plant health/growth analysis by tuning qwen2.5vl:3b", "More accurate plant-health insights", "Worked on tuning the vision model", "Core value of the agritech vision feature", "None reported", "Train CNN for plant disease"],
    ["G N", "2026-06-08", "Vfarm (Vision POC / AI)", "Train a CNN for plant disease detection", "Automated disease detection", "Started CNN training", "Detects crop disease early", "None reported", "Run OpenCV → PlantCV → CNN on Pi"],
    ["G N", "2026-06-09", "Vfarm (Vision POC / AI)", "Run OpenCV → PlantCV → CNN pipeline on the Pi device", "End-to-end on-device vision pipeline", "Vision pipeline running on the Pi", "Validates the edge-inference path", "None reported", "Send sensor logs to New Relic"],
    ["G N", "2026-06-10", "Vfarm (Observability)", "Send sensor logs to New Relic", "Sensor telemetry observable in New Relic", "Started sensor-log integration", "Monitor device/sensor health centrally", "None reported", "Build server logs in New Relic"],
    ["G N", "2026-06-11", "Vfarm (Observability)", "Build server logs in New Relic; add ESP32 logs and Vision logs", "Unified logging across server, ESP32, and vision", "Server log built in New Relic; ESP32 and vision logs added", "Full-stack observability for IoT", "None reported", "Test all API logs and enhance observability"],
    ["G N", "2026-06-12", "Vfarm (Observability)", "Test all API logs and enhance observability", "Verified, richer telemetry", "Began API-log testing and observability enhancements", "Reliable monitoring before scaling", "None reported", "Continue observability hardening"],

    # ---------------- Jegan — Team lead (cross-project) ----------------
    ["Jegan", "2026-05-11", "Cross-project", "Set baseline with Soma; fine-tune real-estate agent; set up WhatsApp backend server; fix Impulse IOT bug", "Kicks off several workstreams", "Day plan set across four workstreams", "Aligns priorities across projects", "None reported", "Report on the day's progress"],
    ["Jegan", "2026-05-11", "Cross-project", "Baseline with Soma; WhatsApp server setup + design docs; agent fine-tuning; Impulse IOT", "Progress on baseline and WhatsApp; design docs prepared", "Baseline done; WhatsApp server setup started and docs cleaned up", "WhatsApp backend is a key deliverable", "Agent fine-tuning not worked; IOT work not done", "Impulse IOT bug; WhatsApp test cases with Docker"],
    ["Jegan", "2026-05-12", "Impulse IOT / WhatsApp", "Fix Impulse IOT bug; add WhatsApp test cases and run locally with Docker", "Bug fix plus test coverage", "Plan set for IOT bug and WhatsApp tests", "Quality and reliability", "None reported", "Backend cleanup; Vfarm API contract"],
    ["Jegan", "2026-05-13", "WhatsApp / Impulse IOT / Vfarm", "Finish WA API backend-cleanup plan and rebase; Impulse IOT bug fix; finalize Vfarm API contract", "Cleaner backend and a defined API contract", "Targeted backend cleanup, IOT fix, and Vfarm API contract", "Stable foundation for WhatsApp and Vfarm", "None reported", "Reel work; landing page; Vfarm"],
    ["Jegan", "2026-05-14", "Reel Agent / ComDove / Vfarm", "Pick up reel work; finalize WhatsApp landing page; Vfarm next steps", "Marketing reels and a launch-ready landing page", "Reel work picked up; landing page targeted", "Supports product launch and marketing", "None reported", "Documentation plan; ComDove launch prep"],
    ["Jegan", "2026-05-15", "Vfarm / ComDove", "Plan Vfarm documentation; prepare ComDove app submission for soft launch; sync with Soma on task creation", "Launch readiness and a docs plan", "Targeted docs plan and app-submission prep", "ComDove soft-launch milestone", "None reported", "Confirm Vfarm/ComDove/Soma items done"],
    ["Jegan", "2026-05-15", "Vfarm / ComDove", "Vfarm task done; attach ComDove domain; Soma sync done", "Domain attached and syncs complete", "ComDove domain attached; Vfarm and Soma items done", "Progress toward ComDove launch", "None reported", "Latency check; connect with Soma"],
    ["Jegan", "2026-05-18", "Cross-project", "Check latency; connect with Soma", "Performance check and team alignment", "Latency check and Soma sync planned", "Performance and coordination", "None reported", "Folder-structure walkthrough and client delivery"],
    ["Jegan", "2026-05-19", "Client Delivery", "Walk client through folder-structure changes and deliver; sync with Soma", "Client receives the restructured deliverable", "Folder-structure changes prepared for client delivery", "Client handoff", "None reported", "Review MQTT; add document types"],
    ["Jegan", "2026-05-20", "Vfarm", "Review MQTT; add more document types", "Validated MQTT and broader document support", "Targeted MQTT review and document types", "IoT messaging quality and flexibility", "None reported", "Complete MQTT review"],
    ["Jegan", "2026-05-20", "Vfarm", "Reviewed MQTT; add documentation (in progress)", "MQTT validated; documentation advancing", "MQTT reviewed; documentation in progress", "Confirms IoT messaging works; captures knowledge", "Documentation not yet complete", "Finish documentation"],
    ["Jegan", "2026-06-02", "BHA / ComDove", "Log work for BHA; WhatsApp screencast", "Tracked BHA work and demo material", "BHA logging and WhatsApp screencast tasks set", "Progress tracking and product demo", "None reported", "Submit WhatsApp for review"],
    ["Jegan", "2026-06-03", "ComDove (WhatsApp)", "Submit WhatsApp (Meta) app for review", "Required Meta approval to go live", "WhatsApp app submitted for review", "Gate to production WhatsApp messaging", "Awaiting Meta review", "Video agent; BHA plan"],
    ["Jegan", "2026-06-04", "Reel/Video Agent / BHA", "Video agent fine-tuning; plan prep for BHA; data validation", "Better video agent, BHA planning, data quality", "Targeted video-agent tuning, BHA plan, data validation", "Improves AI video output and planning", "None reported", "Reel agent video; SSOT e2e flow"],
    ["Jegan", "2026-06-10", "Reel Agent / SSOT", "Work on reel agent new video and fine-tune; prepare SSOT e2e flow", "Improved reel agent and a documented end-to-end flow", "Targeted reel-agent tuning and SSOT flow", "Content automation plus process documentation", "None reported", "Review Alai output"],

    # ---------------- Raghul — DOB site + EMI provider pipeline ----------------
    ["Raghul", "2026-05-11", "EMI Provider Pipeline", "Collect action reviews for Kuhl (Hirocom); run remaining provider batch to fill data gaps", "More complete provider review data", "Planned Kuhl review collection and provider batch run", "Data completeness for the directory", "None reported", "Report batch progress"],
    ["Raghul", "2026-05-11", "EMI Pipeline / DOB Site", "Restart failed EMI provider pipeline batch; extract 6 TripAdvisor reviews; add AI chat to DOB frontend; fix Support channel section on provider details", "Recovered pipeline, more reviews, live AI chat, bug fixed", "Pipeline restarted; 6 reviews extracted; AI chat added; support section fixed", "Keeps data flowing and improves DOB UX", "Earlier pipeline batch had failed (now recovered)", "Verify pipeline; extract more reviews; fine-tune chat"],
    ["Raghul", "2026-05-12", "EMI Pipeline / DOB Site", "Verify restarted pipeline (8 providers) and start next EMI batch; extract remaining TripAdvisor reviews; fine-tune DOB AI chat", "Continuity of data pipeline plus better chat", "Targeted pipeline verification, next batch, and chat tuning", "Reliable data flow and improved DOB assistant", "None reported", "Report extraction and pipeline results"],
    ["Raghul", "2026-05-12", "EMI Pipeline / DOB Site", "Extracted 60/147 TripAdvisor reviews; fine-tuned DOB AI chat box and wired CTA buttons; minor card/about/CTA updates; batch 1 pipeline 19/20 done", "Reviews captured, polished DOB UX, near-complete batch", "60/147 reviews extracted; chat box tuned and CTAs wired; batch 1 19/20 done", "Improves data coverage and site conversion", "20th provider in batch 1 still in progress", "Finish remaining reviews; Compare page design"],
    ["Raghul", "2026-05-13", "DOB Site / EMI Pipeline", "Extract remaining of 60/147 reviews; design Compare page and get client approval, then implement on DOB; verify loop and start next EMI batch", "New Compare feature plus full review coverage", "Targeted remaining reviews, Compare page design, next batch", "Compare page drives user decision-making", "None reported", "Implement Compare page; report batch"],
    ["Raghul", "2026-05-13", "DOB Site / EMI Pipeline", "Extracted 146/146 TripAdvisor reviews; Compare page designed and implementing on DOB; EMI batch 2 running, 21 providers completed", "Full review set, Compare page underway, batch progressing", "146/146 reviews extracted; Compare page implemented; 21 providers done", "Complete data and a key new page", "None reported", "Fix AI chat provider cards; extract Google reviews"],
    ["Raghul", "2026-05-14", "DOB Site / EMI Pipeline", "Verify loop and start next EMI batch; fix AI chat box to add provider cards on DOB; plan and extract Kuhl's 146 Google reviews", "Richer chat results and Google review data", "Targeted batch run, chat provider cards, Google review extraction", "Better assistant answers and review coverage", "None reported", "Report batch and Google review progress"],
    ["Raghul", "2026-05-14", "EMI Pipeline / DOB Site", "EMI batch 2 running (26 completed, 4 blocked, 2 deleted); extracting Kuhl's 19/146 Google reviews", "More providers processed and Google reviews started", "26 providers completed; 19/146 Google reviews extracted", "Grows the verified provider dataset", "4 providers blocked, 2 deleted in batch 2", "Continue batch; finish Google review extraction"],
    ["Raghul", "2026-05-15", "EMI Pipeline / DOB Site", "Verify loop and start next EMI batch; fix AI chat provider cards; upload Kuhl's 146 Google reviews to sheet", "Continued data growth and chat improvements", "Targeted batch run, chat cards, review upload", "Keeps dataset and assistant current", "None reported", "Report batch and upload results"],
    ["Raghul", "2026-05-15", "EMI Pipeline / DOB Site", "EMI batch 2 (31 completed, 7 blocked, 2 deleted); Kuhl 146/146 Google reviews extracted and uploaded; fixed DOB mobile + web responsiveness", "Full Google review set and responsive site", "31 providers done; 146/146 Google reviews uploaded; responsiveness fixed", "Complete review data and usable site on all devices", "7 providers blocked, 2 deleted in batch 2", "Fix Compare page AI verdict; run review analysis"],
    ["Raghul", "2026-05-18", "DOB Site / EMI Pipeline", "Verify loop and start next EMI batch; fix Compare page AI verdict on DOB; run 'paris syndrome' analysis for Kuhl's ghost-town reviews", "Accurate Compare verdicts and review insight", "Targeted batch run, Compare verdict fix, review analysis", "Trustworthy comparisons and review intelligence", "None reported", "Report verdict fix and analysis"],
    ["Raghul", "2026-05-19", "EMI Pipeline / SEO", "Verify loop and start next EMI batch; keyword research for EMI site", "Continued data growth and SEO groundwork", "Targeted batch run and keyword research", "Data coverage plus search visibility", "None reported", "Report batch and keyword findings"],
    ["Raghul", "2026-05-19", "EMI Pipeline / DOB Site", "EMI batch 2 (43 completed, 7 blocked, 3 deleted); DOB frontend bug fixes (Compare page, search filters)", "More providers processed and fewer site bugs", "43 providers done; DOB Compare and search-filter bugs fixed", "Larger dataset and smoother site UX", "7 providers blocked, 3 deleted in batch 2", "Continue batch; more keyword research"],
    ["Raghul", "2026-05-20", "EMI Pipeline / SEO", "Verify loop and start next EMI batch; keyword research for EMI site", "Continued data growth and SEO work", "Targeted batch run and keyword research", "Data coverage plus search visibility", "None reported", "Report batch and keyword findings"],
    ["Raghul", "2026-05-20", "EMI Pipeline / DOB Site", "EMI batch 2 (52 completed, 6 blocked, 2 deleted); DOB frontend bug fixes (provider details, search filters, issue/review button)", "More providers processed and added review button", "52 providers done; provider-details and search fixes; issue/review button added", "Bigger dataset and better engagement features", "6 providers blocked, 2 deleted in batch 2", "Continue batch; DOB bug fixes"],
    ["Raghul", "2026-05-21", "EMI Pipeline / DOB Site", "Verify loop and start next EMI batch; DOB frontend bug fixes", "Continued data growth and site polish", "Targeted batch run and DOB bug fixes", "Data coverage and site quality", "None reported", "Report batch and fixes"],
    ["Raghul", "2026-05-21", "EMI (Content Agent)", "Create an agent for Compare-page content generation for EMI", "Automated Compare-page content", "Started building the Compare-page content agent", "Scales content creation without manual effort", "None reported", "Finish and run the content agent"],
    ["Raghul", "2026-05-22", "DOB Site", "Fix alignment on DOB Compare page; create About, Privacy, and Terms pages", "Polished Compare page and required legal pages", "Targeted Compare-page alignment and legal pages", "Professional, compliant site", "None reported", "Implement pages; verify alignment"],
    ["Raghul", "2026-05-25", "EMI (Logo Agent)", "Create the logo agent for EMI and start the loop", "Automated provider-logo collection", "Logo agent created and loop started", "Logos improve provider presentation", "None reported", "Run logo agent; map provider logos"],
    ["Raghul", "2026-05-26", "EMI / DOB Site", "Run logo agent to save all logos and map provider logo per provider; add Trustpilot and Google reviews to each provider details page", "Complete logos and multi-source reviews on details pages", "Targeted logo run, logo mapping, and review additions", "Richer, more credible provider pages", "None reported", "Report logo and review results"],
    ["Raghul", "2026-05-26", "EMI / DOB Site", "Ran logo agent and saved all logos; fixed EMI provider pipeline; created agent for review pull from Trustpilot and Google", "Complete logos, fixed pipeline, automated review pulls", "All logos saved; pipeline fixed; review-pull agent created", "Automates review aggregation and stabilizes pipeline", "None reported", "Map provider logos and reviews"],
    ["Raghul", "2026-05-27", "EMI / DOB Site", "Map provider logo and reviews for each provider", "Logos and reviews linked to each provider", "Targeted logo and review mapping", "Complete, credible provider profiles", "None reported", "On-page SEO work"],
    ["Raghul", "2026-06-01", "DOB Site (SEO)", "Fix on-page SEO on DOB site", "Better search ranking for DOB", "On-page SEO fixes on DOB site", "Drives organic traffic", "None reported", "Continue on-page SEO"],
    ["Raghul", "2026-06-02", "DOB Site (SEO)", "Fix on-page SEO on DOB site", "Better search ranking for DOB", "On-page SEO fixes on DOB site", "Drives organic traffic", "None reported", "SEO plus data dedupe"],
    ["Raghul", "2026-06-02", "DOB Site (SEO) / EMI Data", "Fix on-page SEO on DOB site; work on dedupe of stale provider data", "Better ranking and cleaner data", "On-page SEO fixed; began dedupe of stale provider data", "Search visibility and data quality", "None reported", "Evaluate and fix EMI data duplication"],
    ["Raghul", "2026-06-03", "EMI Data", "Evaluate EMI provider data and fix duplication", "Clean, de-duplicated provider data", "Targeted EMI data evaluation and dedupe", "Accurate directory without duplicates", "None reported", "Continue dedupe; DOB SEO"],
    ["Raghul", "2026-06-04", "EMI Data / DOB SEO", "Evaluate EMI provider data and fix duplication; DOB site SEO", "Clean data and better ranking", "Targeted EMI dedupe and DOB SEO", "Data quality and search visibility", "None reported", "Continue dedupe and SEO"],
    ["Raghul", "2026-06-05", "EMI Data / DOB SEO", "Evaluate EMI provider data and fix duplication; DOB site SEO", "Clean data and better ranking", "Targeted EMI dedupe and DOB SEO", "Data quality and search visibility", "None reported", "Continue dedupe and SEO"],
    ["Raghul", "2026-06-08", "EMI Data / DOB SEO", "Evaluate EMI provider data and fix duplication; DOB site SEO", "Clean data and better ranking", "Targeted EMI dedupe and DOB SEO", "Data quality and search visibility", "None reported", "Continue dedupe; SEO section 9"],
    ["Raghul", "2026-06-09", "EMI Data / DOB SEO", "Evaluate EMI provider data and fix duplication; DOB site SEO section 9", "Clean data and structured SEO", "Targeted EMI dedupe and SEO section 9", "Data quality and search visibility", "None reported", "Migrate and test SEO section 9"],
    ["Raghul", "2026-06-09", "DOB SEO / EMI Data", "SEO section 9: migration done and tested with 1 provider; verified 3S Money provider with Soma", "Validated SEO migration approach", "SEO section 9 migration tested on 1 provider; 3S Money verified", "Proves SEO migration before full rollout", "None reported", "Optimize SEO section 9 for all parameters"],
    ["Raghul", "2026-06-10", "EMI Data / DOB SEO", "Evaluate EMI provider data and fix duplication; optimize DOB SEO section 9 to cover all needed parameters", "Clean data and complete SEO coverage", "Targeted EMI dedupe and SEO section 9 optimization", "Data quality and full SEO compliance", "None reported", "Complete SEO section 9 optimization"],
    ["Raghul", "2026-06-17", "DOB Site", "Implement 'best for' page details on the DOB site", "Clearer provider positioning for users", "Started 'best for' page-details implementation", "Helps users pick the right provider", "None reported", "Finish page-details implementation"],

    # ---------------- Sahil Thakur — ComDove (WhatsApp Business platform) ----------------
    ["Sahil Thakur", "2026-05-11", "ComDove (WhatsApp)", "Embedded Signup: capture and persist fbActorUserId on callback; add backend logging", "Stores Facebook user ID for later deletion; better server visibility", "Started fbActorUserId capture and backend logging", "Needed for data-deletion compliance and debugging", "None reported", "Confirm capture and logging work"],
    ["Sahil Thakur", "2026-05-11", "ComDove (WhatsApp)", "Captured fbActorUserId from embedded signup callback and stored in DB; added backend logging", "Compliant user-ID storage and traceable server flow", "fbActorUserId captured and stored; backend logging added", "Enables data deletion and easier debugging", "None reported", "Set up cloud PostgreSQL DB"],
    ["Sahil Thakur", "2026-05-12", "ComDove (Infra)", "Set up PostgreSQL DB on AWS; get connection credentials to connect locally", "Cloud database for the platform", "Started AWS PostgreSQL setup", "Production-grade data storage", "None reported", "Complete DB setup; fix outstanding bugs"],
    ["Sahil Thakur", "2026-05-12", "ComDove (Infra/Bugfix)", "Set up PostgreSQL on AWS and get creds; fix Facebook re-connect bug; fix 'Could not load teammates/members' error in Inbox and Settings", "Working cloud DB and two key bugs fixed", "PostgreSQL set up; Facebook re-connect and teammates-load bugs fixed", "Stable login and team management", "None reported", "Build legal pages; fix UI responsiveness"],
    ["Sahil Thakur", "2026-05-13", "ComDove (Web)", "Create Privacy & Terms page; add footer links to Privacy/Terms/Data Deletion on every page; fix UI responsiveness", "Legal compliance and consistent navigation", "Started legal pages, footer links, and responsiveness fixes", "Required for Meta approval and usability", "None reported", "Finish legal pages; deploy backend"],
    ["Sahil Thakur", "2026-05-13", "ComDove (Web/Infra)", "Created Privacy, Terms, About, Status, and Data Deletion pages; fixed responsive design; deployed WhatsApp backend on AWS Lightsail", "Compliant pages, responsive site, deployed backend", "Legal pages created; responsiveness fixed; backend deployed on Lightsail", "Meets compliance and gets backend live", "None reported", "Convert CSS to Tailwind; fix UI"],
    ["Sahil Thakur", "2026-05-14", "ComDove (Web)", "Convert all CSS to Tailwind; fix UI issues", "Maintainable styling and cleaner UI", "Started Tailwind conversion and UI fixes", "Easier styling maintenance", "None reported", "Finish Tailwind conversion; responsiveness"],
    ["Sahil Thakur", "2026-05-14", "ComDove (Web/Infra)", "Made all pages responsive; converted CSS to Tailwind; connected backend logs to CloudWatch; created Contact page", "Responsive Tailwind UI, centralized logs, Contact page", "Pages responsive; Tailwind done; logs in CloudWatch; Contact page created", "Better UX, observability, and lead capture", "None reported", "Connect domain via Route 53"],
    ["Sahil Thakur", "2026-05-15", "ComDove (Infra)", "Connect Route 53 with the domain", "Custom domain for the platform", "Started Route 53 domain connection", "Branded, production URL", "None reported", "Complete DNS setup; UI fixes"],
    ["Sahil Thakur", "2026-05-15", "ComDove (Infra/Web)", "Connected Route 53 with production domain (DNS); fixed UI (logo, heading, pages); created n8n workflow for contact form → Slack", "Live domain, polished UI, automated lead alerts", "Route 53 connected; UI fixed; n8n contact-form-to-Slack workflow created", "Production domain plus instant lead notifications", "None reported", "Fix inbox reply display bug"],
    ["Sahil Thakur", "2026-05-18", "ComDove (Inbox)", "Debug and fix incoming WhatsApp reply messages not showing in inbox", "Reliable inbound message display", "Started inbox reply-display debugging", "Core messaging must show all replies", "None reported", "Confirm fix; dashboard work"],
    ["Sahil Thakur", "2026-05-18", "ComDove (Dashboard/Contacts)", "Fixed dashboard page and created new dashboard APIs; created contact table and contact APIs", "Working dashboard and contact data model", "Dashboard fixed with new APIs; contact table and APIs created", "Foundation for dashboard and contacts features", "None reported", "Add send-message sidebar on Contacts"],
    ["Sahil Thakur", "2026-05-19", "ComDove (Contacts/Inbox)", "Add Send Message sidebar on Contacts page with a form to send WhatsApp messages; on success show the conversation in Inbox automatically", "Send messages directly from Contacts", "Started Send Message sidebar and inbox auto-display", "Streamlines outbound messaging", "None reported", "Test send flow; point frontend to new API"],
    ["Sahil Thakur", "2026-05-21", "ComDove (Frontend/Infra)", "Update frontend to use new API URL (api.comdove.com) and test all endpoints", "Frontend wired to production API", "Started frontend API-URL switch and endpoint testing", "Connects UI to the live backend", "None reported", "Fix account-role and campaign planning"],
    ["Sahil Thakur", "2026-05-22", "ComDove (Auth/Campaign)", "Fix account-creation default role (should be Admin, not Team); plan WhatsApp campaign; research adding more WhatsApp templates", "Correct roles and campaign roadmap", "Targeted role fix, campaign plan, and template research", "Correct permissions and growth features", "New users were getting Team role instead of Admin", "Build templates page; campaign work"],
    ["Sahil Thakur", "2026-05-25", "ComDove (Templates)", "Add templates page", "Manage WhatsApp message templates", "Templates page added", "Templates speed up messaging", "None reported", "Fix signup/invite roles; plan media headers"],
    ["Sahil Thakur", "2026-05-26", "ComDove (Auth/Templates)", "Fixed signup landing (now admin page); fixed team-invite role (now saved as Team); created plan for WhatsApp template media headers (image/video/document)", "Correct roles and richer template plan", "Signup landing and invite-role bugs fixed; media-headers plan created", "Correct permissions and richer templates", "None reported", "Implement template media headers"],
    ["Sahil Thakur", "2026-05-27", "ComDove (Templates)", "Start implementing WhatsApp template media headers", "Templates support image/video/document", "Started template media-header implementation", "Richer message templates", "None reported", "Continue media-header implementation"],
    ["Sahil Thakur", "2026-06-01", "ComDove (Campaign)", "Start implementing WhatsApp campaign", "Bulk WhatsApp campaign capability", "Started campaign implementation", "Enables marketing campaigns", "None reported", "Build campaign API and table"],
    ["Sahil Thakur", "2026-06-01", "ComDove (Campaign)", "Completed campaign API and created table migration for the campaign table", "Backend ready for campaigns", "Campaign API completed; campaign table migration created", "Foundation for campaign feature", "None reported", "Build campaign UI; integrate API"],
    ["Sahil Thakur", "2026-06-02", "ComDove (Campaign)", "Create campaign API (backend endpoints); create campaign UI and integrate with API", "End-to-end campaign feature", "Targeted campaign API and UI integration", "Delivers usable campaign feature", "None reported", "Confirm campaign page works"],
    ["Sahil Thakur", "2026-06-02", "ComDove (Campaign)", "Created campaign page and implemented campaign API", "Working campaign feature", "Campaign page created and API implemented", "Users can run campaigns", "None reported", "Add contact import/export"],
    ["Sahil Thakur", "2026-06-03", "ComDove (Contacts)", "Add import/export on Contacts page — upload CSV to import contacts", "Bulk contact management", "Started CSV import/export on Contacts", "Faster contact onboarding", "None reported", "Add group field; selective export"],
    ["Sahil Thakur", "2026-06-04", "ComDove (Contacts)", "Add contact-group field in CSV import; fix export to allow selective contact export", "Grouped imports and targeted exports", "Started contact-group import and selective export", "More control over contact data", "None reported", "Plan automation feature"],
    ["Sahil Thakur", "2026-06-08", "ComDove (Automation)", "Analyze and create an implementation plan for the Automation feature using Windmill", "Roadmap for workflow automation", "Automation/Windmill implementation plan created", "De-risks the automation build", "None reported", "Build a Windmill POC"],
    ["Sahil Thakur", "2026-06-09", "ComDove (Automation)", "Build a POC: simple React project connected to Windmill via webhook", "Proves React-to-Windmill integration", "Started React + Windmill webhook POC", "Validates the automation architecture", "None reported", "Connect React Flow UI to Windmill API"],
    ["Sahil Thakur", "2026-06-10", "ComDove (Automation)", "Connect completed React Flow UI to Windmill API — push the user-created automation flow to Windmill on save/submit", "User-built flows execute in Windmill", "Started React Flow UI → Windmill API connection", "Core of the automation feature", "None reported", "Ensure AI agent handles messages"],
    ["Sahil Thakur", "2026-06-11", "ComDove (AI Agent)", "Ensure the AI agent correctly receives and responds to messages", "Reliable AI-assisted replies", "Started AI agent message handling", "Automated, accurate responses", "None reported", "Write SRS for Automation feature"],
    ["Sahil Thakur", "2026-06-17", "ComDove (Automation)", "Write the SRS plan for the Automation (Windmill) feature", "Clear spec for the automation feature", "SRS plan for Automation drafted", "Aligns build with requirements", "None reported", "Build the Automation Builder UI"],
    ["Sahil Thakur", "2026-06-17", "ComDove (Automation)", "Automation Builder UI is ready", "Visual automation builder available", "Automation Builder UI completed", "Lets users design flows visually", "None reported", "Wire builder to backend execution"],

    # ---------------- Soma Pani — Operations / SSOT / Agents ----------------
    ["Soma Pani", "2026-05-11", "Operations / SSOT", "Understand outreach flow end-to-end; prepare Project Operational Checklist", "Shared understanding and an operational checklist", "Outreach flow reviewed; operational checklist prepared", "Aligns operations and onboarding", "None reported", "Claude skill development; SSoT docs"],
    ["Soma Pani", "2026-05-14", "SSOT / Agents", "Claude skill development and setup; improve SSoT documentation structure; review and organize project workflow/documentation standards", "Reusable Claude skills and cleaner SSoT docs", "Started Claude skill setup and SSoT documentation improvements", "Standardized process and tooling", "None reported", "Design and build agents"],
    ["Soma Pani", "2026-05-15", "Agents", "Work on creating and designing agents; explore agent workflow and implementation structure", "Foundation for the agent framework", "Began agent design and workflow exploration", "Agents power downstream automation", "None reported", "Implement agent workflow"],
    ["Soma Pani", "2026-06-01", "SSOT / Jira Automation", "Read Jira story WS-95; fetch all subtasks; verify all subtasks Done; generate completion report; update SSOT documents (completed and tested)", "Automated story verification and reporting", "WS-95 subtasks verified, completion report generated, SSOT updated, completed and tested", "Reliable, automated project tracking", "None reported", "Apply the flow to more Jira stories"],
]


def build_rows() -> tuple[list[list[str]], int]:
    """De-duplicate the source rows and append the derived Status column.

    Returns the deduplicated rows (each with Status as the last cell) and the
    number of exact-duplicate rows dropped (NFR-4-style idempotency).
    """
    seen: set[tuple] = set()
    rows: list[list[str]] = []
    for row in ROWS:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        # Status keyed by Developer Name (col 1) + Date (col 2); default when no check-in.
        status = STATUS_BY_KEY.get((row[0], row[1]), NO_CHECKIN)
        rows.append(row + [status])
    return rows, len(ROWS) - len(rows)


def write_csv(values: list[list[str]], path: Path) -> None:
    """Write the header+rows to a UTF-8 CSV (importable into Google Sheets)."""
    path.parent.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(values)


def _rgb(hex_color: str) -> dict:
    """Convert 'RRGGBB' to a Sheets API color dict."""
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def push_to_sheets(values: list[list[str]]) -> str | None:
    """Upsert the values into the configured Google Sheet; return its URL.

    Returns None (and prints guidance) if Google credentials/config are absent,
    so the CSV is still produced. Re-running clears and rewrites the tab, so the
    push is idempotent — no duplicate rows.
    """
    key_path = Path(os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json"))
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    tab = os.environ.get("SHEET_TAB_NAME", "Standup").strip() or "Standup"

    if not spreadsheet_id or not key_path.exists():
        print(
            "Skipping Google Sheets push — not configured.\n"
            f"  - service-account key: {'found' if key_path.exists() else 'MISSING at ' + str(key_path)}\n"
            f"  - SPREADSHEET_ID: {'set' if spreadsheet_id else 'MISSING'}\n"
            "  Set GOOGLE_SA_KEY_PATH + SPREADSHEET_ID in .env and share the sheet\n"
            "  with the service-account email as Editor, then re-run."
        )
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(str(key_path), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=len(values) + 10, cols=len(HEADERS))

    # Idempotent write: wipe the tab, then write all values from A1 (FR-13/FR-14).
    ws.clear()
    ws.update(values=values, range_name="A1", value_input_option="RAW")

    _apply_formatting(sh, ws, len(values))
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={ws.id}"


def _apply_formatting(sh, ws, total_rows: int) -> None:
    """Freeze + bold the header and colour-code the Status column. Best-effort."""
    try:
        last_col = chr(ord("A") + len(HEADERS) - 1)  # "J" for 10 columns
        ws.freeze(rows=1)
        ws.format(
            f"A1:{last_col}1",
            {
                "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")},
                "backgroundColor": _rgb("1F4E78"),
                "horizontalAlignment": "CENTER",
            },
        )

        status_col0 = len(HEADERS) - 1  # zero-based index of the Status column
        rng = {
            "sheetId": ws.id,
            "startRowIndex": 1,
            "endRowIndex": total_rows,
            "startColumnIndex": status_col0,
            "endColumnIndex": status_col0 + 1,
        }
        # Drop any conditional-format rules left from a previous run (idempotency).
        existing = next(
            (s for s in sh.fetch_sheet_metadata().get("sheets", []) if s["properties"]["sheetId"] == ws.id),
            {},
        ).get("conditionalFormats", [])
        requests = [
            {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}}
            for _ in existing
        ]
        for text, hex_color in STATUS_FILL.items():
            requests.append(
                {
                    "addConditionalFormatRule": {
                        "index": 0,
                        "rule": {
                            "ranges": [rng],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_EQ",
                                    "values": [{"userEnteredValue": text}],
                                },
                                "format": {"backgroundColor": _rgb(hex_color)},
                            },
                        },
                    }
                }
            )
        sh.batch_update({"requests": requests})
    except Exception as exc:  # noqa: BLE001 — formatting must never fail the data push
        print(f"  (formatting skipped: {exc})")


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")

    rows, dropped = build_rows()
    values = [HEADERS] + rows

    csv_path = Path(__file__).parent / "Result" / "standup_report.csv"
    write_csv(values, csv_path)
    print(f"Wrote {len(rows)} rows ({dropped} duplicate(s) dropped) -> {csv_path}")

    url = push_to_sheets(values)
    if url:
        print(f"Pushed {len(rows)} rows to Google Sheets -> {url}")


if __name__ == "__main__":
    main()
