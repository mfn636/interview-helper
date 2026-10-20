# Interview Helper

A Python desktop assistant for **computer science and software engineering interviews**, covering AI application development, Python, Java, JavaScript, Node.js, full-stack development, databases, networking, operating systems, and related technical topics.

Its central feature is **switching between answering and asking questions**. Use the same resume, job description (JD), and project preferences to have the AI respond as a candidate or conduct a mock interview as an interviewer.

The project has undergone **automated regression testing and two live, 20-question evaluation runs**. Findings from difficult cases have been collected and used to refine prompt constraints and response examples. The latest offline run passed **167 tests**. See [Tests, evaluation, and regression](#tests-evaluation-and-regression) for the results, scope, and retained regression material.

## Answerer and interviewer modes

| AI role | Treats incoming speech as | Produces |
| --- | --- | --- |
| **Candidate / Answerer** (default) | An interviewer's question | A direct, conversational answer in one paragraph, using familiar and relevant projects when appropriate |
| **Interviewer / Questioner** | A candidate's statement or answer | One main question or follow-up based on the resume, JD, and conversation, without volunteering a model answer |

Use the role button above the conversation to switch modes. Switching **pauses listening and clears the conversation, transcription, and pending queue**. Loaded interview materials, readiness, and confirmed project preferences are retained. Start listening again to continue with the new role. Switching is disabled while initialization or a response is in progress; restarting defaults to Candidate mode.

The current desktop UI, bundled prompts, example materials, and question bank are primarily in Chinese. This README uses English descriptions of the controls. The role prompts support explicit requests for English responses.

## Features

- **Live system-audio transcription:** captures sound played through a Windows output device, transcribes it through iFLYTEK, and submits finalized speech after a pause.
- **Dark floating window:** supports always-on-top mode, opacity adjustment, resizing, and copying the latest answer or question.
- **Direct interview context:** stores one resume and one JD in separate Markdown files and includes them with the role prompt, project preferences, and conversation history. No vector database is required.
- **Explicit project preferences:** detects projects from the resume and asks the user to confirm their order from most to least familiar before the interview starts.
- **Multiple conversation turns:** queues finalized speech while the AI responds and supports retrying or skipping failed requests.
- **Conversational output:** uses single-paragraph responses, factual boundaries, and few-shot examples to distinguish documented experience from hypothetical approaches.
- **Tests and regression material:** includes deterministic tests, a fixed 20-question bank with scoring criteria, and prompt rules informed by reviewed failure cases.

The default model is **DeepSeek Flash** (`deepseek-flash`), with thinking disabled. Speech recognition uses iFLYTEK's **standard real-time transcription service (RTASR)**.

## Quick start

Desktop audio features require **Windows and Python 3.11 or 3.12**, an available audio output device, and Tkinter. Official Windows Python installers normally include Tkinter.

Run in PowerShell:

```powershell
git clone https://github.com/mfn636/interview-helper.git
cd interview-helper
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item context/resume.example.md context/resume.md
Copy-Item context/job_description.example.md context/job_description.md
```

For Python 3.12, replace `-3.11` with `-3.12`. The copy commands are for first-time setup; if you already have local configuration or materials, edit those files instead of replacing them.

Fill in these UTF-8 files:

| File | Contents |
| --- | --- |
| `.env` | Your DeepSeek API key and iFLYTEK RTASR APPID/APIKey |
| `context/resume.md` | The candidate's name, resume, project experience, and additional background |
| `context/job_description.md` | The target job title, responsibilities, and requirements |

The supplied `.example.md` files are **entirely fictional demonstrations**. Replace them with the materials for your interview before using the assistant with real personal background.

Launch the floating window:

```powershell
.\.venv\Scripts\python.exe main.py
```

## API configuration

Put your own credentials in the local `.env` file. These placeholders are not valid credentials:

```dotenv
API_KEY=your_deepseek_api_key

XFYUN_APP_ID=your_xfyun_app_id
XFYUN_API_KEY=your_xfyun_rtasr_api_key

SPEECH_SILENCE_MS=1500
SPEECH_RMS_THRESHOLD=180
SPEECH_MAX_SEGMENT_MS=50000
```

The iFLYTEK APPID and APIKey must belong to the same application with **standard RTASR** enabled. **APISecret is not required.** Speech recognition and model responses use separate services, each subject to its own quota and pricing.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SPEECH_SILENCE_MS` | `1500` | Continuous silence duration before attempting to finish a speech segment |
| `SPEECH_RMS_THRESHOLD` | `180` | PCM16 volume threshold; lower it for quiet speech or raise it to reduce background-noise triggers |
| `SPEECH_MAX_SEGMENT_MS` | `50000` | Internal connection rollover interval for long audio; rollover joins transcripts and does not itself submit a question |

## Interview workflow

1. **Load materials.** Startup reads the role prompt, resume, and JD, then asks the model to identify projects in the resume.
2. **Confirm project order.** Move projects up or down from most to least familiar, then confirm. A single project still needs confirmation. If no projects are detected, check the resume or explicitly confirm that the session should continue without projects.
3. **Wait for readiness.** After confirmation, the AI returns a readiness message containing the candidate's name and target job title. Listening remains disabled until this succeeds. Deferring project confirmation keeps the interview unready; reopen the project dialog from the main window when needed.
4. **Choose a role and audio output.** Candidate mode is the default. Use the role button to switch to Interviewer mode and select the output device playing the speech you want to transcribe.
5. **Start listening.** Partial text updates live. Once a pause and finalized transcription are received, the text enters the queue and the AI responds in the selected role.

Both roles capture **the selected system audio output**. Switching roles does not change the source to a microphone. In Interviewer mode, the candidate's speech must be played through that output. For text-based practice, use the CLI or Python client below.

Project preferences remain in memory for the current run. Explicit references to a project and ongoing follow-ups take precedence over the preference order. Preferences do not alter resume facts or turn JD requirements into claimed experience.

If materials change before readiness, use the reload-materials control to read them again and reconfirm the projects. An ordinary initialization retry preserves the confirmed order. After readiness, restart the program to change materials.

### Listening and conversation controls

- **Pause listening:** stops capture and automatic submission, discards unfinished speech, and retains finalized queued input. An active AI response may finish; the queue resumes when listening restarts.
- **Retry or skip:** a failed request pauses subsequent submissions. Failed exchanges are not added to history; retries wait for listening to resume if it is paused.
- **New conversation:** stops listening and clears history, transcripts, and the queue while retaining materials, project order, and the current role. Late results from the old conversation are ignored.
- **Refresh devices:** updates the output-device list. Pause listening before changing devices.
- **Window controls:** drag the system title bar to move, resize from the edges, toggle always-on-top mode, adjust opacity from 65% to 100%, or copy the latest response. Closing the window stops capture.

List output devices without recording:

```powershell
.\.venv\Scripts\python.exe main.py --list-audio-devices
```

## CLI and Python usage

The CLI accepts typed input, uses Candidate mode by default, and does not capture audio:

```powershell
.\.venv\Scripts\python.exe main.py --cli
```

Enter a complete project order such as `2 1`; enter `1` for a single project or `none` when there are no projects. `/clear` clears the conversation while retaining materials and preferences; `/exit` quits. The CLI currently has no role-switch command. Use the desktop button or select a role through the Python client:

```python
from deepseek_client import DeepSeekClient
from loop import parse_project_order

client = DeepSeekClient(role="interviewer")  # "candidate" selects the answerer
projects = client.detect_projects()
for number, name in enumerate(projects, 1):
    print(number, name)

order = parse_project_order(input("Project numbers by familiarity, or none: "), projects)
client.set_project_order(order)
print(client.initialize())
print(client.chat("Please start this mock interview in English."))
```

Use `DeepSeekClient(resume_path="my_resume.md", jd_path="my_job.md")` to specify different files. When calling `client.set_role("candidate")` or `client.set_role("interviewer")` directly, clear any conversation history maintained by your code. The desktop session handles this automatically.

## Tests, evaluation, and regression

Testing has included both **software behavior regression tests** and **reviewed model-answer regression on a fixed question set**. These measure different things: offline tests check application behavior; live evaluations examine generated answers and response times.

### Automated regression tests

The latest offline run before the initial public release passed **167 tests**. Coverage includes:

- Role-specific prompts, switching failures, conversation reset, and readiness preservation.
- Separate resume/JD loading, project extraction validation, and explicit familiarity confirmation.
- Speech authentication, transcript updates, audio segmentation, and connection rollover.
- Queue ordering, pause/resume, retry/skip behavior, and rejection of stale results.
- CLI project selection and benchmark setup validation.

Run the suite locally without making live API calls:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

### Completed 20-question evaluation and regression

On **October 18, 2026**, the candidate-answering prompt was evaluated on a fixed set of **20 computer science interview questions**, then tested again on the same questions after adding lightweight accuracy and formatting constraints. Both runs used DeepSeek Flash with thinking disabled, one independent response per question, the same interview materials, and the original scoring criteria.

| Measure | Initial evaluation | Regression after prompt constraints |
| --- | ---: | ---: |
| Successful responses | 20/20 | 20/20 |
| Review score | 83/100 | 94/100 |
| Score on the five initially difficult questions | 15/25 | 21/25 |
| Mean complete-response time | 2.99 s | 3.26 s |
| P95 complete-response time | 3.54 s | 3.95 s |

Scores came from **Codex review against the predefined rubric**, not from DeepSeek grading its own answers. P95 uses the nearest-rank method. Timings cover complete responses, exclude initialization, and are not time-to-first-token measurements.

These are **historical results on known questions**, some of which informed the added constraints. They do not establish accuracy on unseen questions. The runs preceded later few-shot examples and role-switching changes, so **94/100 is not a verified score for the current release or Interviewer mode**.

### Failure cases retained for regression

The evaluation work followed a concrete feedback cycle:

1. Run the fixed question bank and record answers, timing, and token usage.
2. Review each answer against the existing criteria and identify difficult cases.
3. Record the incorrect claim, its cause, a corrected explanation, and checks for future reruns.
4. Add targeted prompt constraints and rerun the same 20 questions to check improvements and remaining errors.
5. Retain difficult cases and add few-shot examples for conversational format, concise follow-ups, version-specific boundaries, and evidence-based personal experience.

The retained archive contains **13 technical cases and two recurring formatting issues**, including resolved problems, remaining gaps, and new regressions. Reviewed topics include InnoDB snapshots and locking reads, composite indexes and index condition pushdown, HashMap treeification, attention complexity, and unsupported claims about a candidate's RAG work. The follow-up run still exposed issues, including personal-experience claims unsupported by the supplied materials. Those cases remain useful regression targets.

The public regression material is retained in:

- [The 20-question bank and scoring criteria](benchmarks/questions.json).
- [The candidate prompt](prompts/interview_assistant.md), including technical boundary rules and four few-shot examples.
- [The offline tests](test_floating_session.py) and other `test_*.py` files for repeatable application behavior checks.

Detailed answer logs, review reports, and the original difficult-case archive stay local because they may contain personal interview context. They are excluded from Git. The archive is a maintenance record, not a runtime knowledge base. Later few-shot additions still require a new live evaluation; adding an example is not itself evidence that the model has passed a regression.

### Reproduce the evaluation

Validate the public question bank without API calls:

```powershell
.\.venv\Scripts\python.exe benchmark.py --validate
```

For a live run, first provide your own credentials and materials, then explicitly select the project familiarity order. This example applies only when two projects are detected and the second is more familiar:

```powershell
.\.venv\Scripts\python.exe benchmark.py --project-order "2,1"
```

Use every detected project exactly once; use `1` for one project or `none` for no projects. Results are saved under `benchmarks/results/`, including raw and normalized answers, newline counts, complete-response timing, token usage, and material fingerprints. Keep the question set, model settings, materials, and scoring criteria consistent when comparing runs. Review answer quality against the rubric separately: the script does not automatically grade technical correctness.

## Project structure and prompts

```text
main.py                            Application entry point
floating_window.py                 Tkinter UI and role switching
floating_session.py                Conversation state and background work
deepseek_client.py                 DeepSeek requests, projects, and role prompts
interview_context.py               Resume and JD loading
project_order_dialog.py            Project familiarity confirmation
speech_session.py                  Speech-session lifecycle
system_audio.py                    Windows system-audio capture
xfyun_rtasr.py                     iFLYTEK RTASR protocol
voice_controller.py                Transcription and response queue
loop.py                            CLI conversation loop
prompts/interview_assistant.md      Candidate / Answerer prompt
prompts/interview_interviewer.md    Interviewer / Questioner prompt
context/resume.example.md          Fictional resume example
context/job_description.example.md Fictional JD example
.env.example                       Credential-free configuration template
benchmark.py                       Live evaluation runner and offline validation
benchmarks/questions.json          Fixed questions and scoring criteria
test_*.py                          Automated regression tests
```

The two role prompts can be edited independently. The candidate prompt encourages a natural progression from conclusion to mechanism to necessary qualifications. The interviewer prompt asks one relevant question or follow-up at a time. The client normalizes extra line breaks without truncating the answer. Prompt constraints do not guarantee factual correctness.

## Current limitations

The complete **system audio -> RTASR -> DeepSeek** live path has not yet been consistently verified as stable. Integration testing encountered handshake timeouts, disconnections, and service error `10700`. Offline regression results and model-answer evaluations do not establish reliability across audio devices and networks. Test transcription with your own device and configuration before a full mock interview. Unfinished speech is not automatically submitted after a recognition failure.

Materials and conversation history are not automatically truncated. Long conversations can increase latency and cost or reach the model's context limit; start a new conversation to clear history.

## Private data and service flow

The repository contains code, prompts, and fictional examples. Real `.env` files, resumes, JDs, original attachments, evaluation outputs, and the private difficult-case archive are excluded through `.gitignore`. Keep credentials out of prompts, screenshots, issues, and commits; add ignore rules for any additional private files you create.

While listening, audio from the selected output device is sent to iFLYTEK, including other audio played on that device. Transcribed text, interview materials, project preferences, and conversation history are sent to DeepSeek. The application does not save recordings to disk. Conversation history and project preferences remain in memory; the benchmark runner separately saves evaluation results.

## License

[MIT License](LICENSE).
