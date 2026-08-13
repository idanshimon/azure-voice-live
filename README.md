# azure-voice-live

Record from your mic and stream it straight to Azure AI Speech to clone your
voice — then stream synthesized speech back and play it as it arrives.

No files on disk, no SDK, no dependencies beyond `ffmpeg` and the Azure CLI.
Two Python files' worth of stdlib `urllib`.

```
./live.py enroll          # ~90 seconds of talking -> your voice, cloned
./live.py say "hello"     # plays in your voice, first audio in under a second
```

Measured on a real call: **first audio chunk at 927 ms**, playback starts while
Azure is still synthesizing.

## The question that probably brought you here

**Can you download the voice model and use it elsewhere? No.**

Azure gives you a `speakerProfileId` — a GUID pointing into your Speech
resource. The embedding never leaves Azure. Microsoft does not ship serialized
model files, weights, or container images for custom voices, and professional
voice (CNV Pro) is worse: the model lives on a deployed endpoint you cannot
export.

What you *can* share with your other services is the endpoint plus the profile
ID. Any language, plain HTTPS, sub-second latency — it behaves like a local
voice, but synthesis always happens on Azure.

If you need a portable artifact you own, use a local model (F5-TTS, XTTS-v2,
Piper) instead. Azure will not give you one at any tier.

## Setup

**1. Prerequisites**

```bash
brew install ffmpeg azure-cli     # ffplay ships with ffmpeg
az login
```

**2. Create a Speech resource** (skip if you have one)

```bash
az group create -n rg-voice -l eastus
az cognitiveservices account create \
  -n my-speech-resource -g rg-voice \
  --kind SpeechServices --sku S0 -l eastus \
  --custom-domain my-speech-resource
```

The custom domain matters — token auth fails without it.

**3. Configure**

```bash
cp .env.example .env
$EDITOR .env
```

**4. Find your mic index**

```bash
ffmpeg -f avfoundation -list_devices true -i ""    # macOS
arecord -l                                        # Linux
```

Put the number in `MIC` in `.env`.

**5. Go**

```bash
./live.py enroll
./live.py say "now it sounds like me"
```

## Recording tips

These are the difference between working and `AudioAndScriptNotMatch`.

**Consent (20s)** — Azure speech-matches your audio against the exact script,
so this is machine-verified, not a formality.

- English only. The locale is pinned to `en-US`. A paraphrase or another
  language fails.
- Read it word for word. `enroll` prints the exact sentence with your name in it.
- Pause a beat after pressing Enter before speaking, and leave silence at the end.
- Normal pace. Rushing hurts the match more than an accent does.

**Voice prompt (60s)** — this defines what the clone sounds like.

- Speak how you want it to sound. Monotone in, monotone out.
- Any language, any content. Read an article, describe your week.
- Quiet room, consistent mic distance. Background noise is the top quality killer.
- Continuous speech beats fragments. 30s of real talking is plenty.

## Two things that will waste your afternoon

**Auth is not uniform across the two APIs.**

```
customvoice management API  ->  Authorization: Bearer <token>
TTS synthesis endpoint      ->  Authorization: Bearer aad#<resourceId>#<token>
```

Plain bearer on the synthesis endpoint returns 401 with nothing useful in the
body. The composite form is undocumented in most samples.

**ffmpeg writes broken WAV headers to a pipe.**

Streaming WAV to stdout, ffmpeg can't seek backward to fill in the RIFF and
data size fields, so it writes `0xFFFFFFFF` for both. Azure rejects the upload
as corrupt. `fix_wav_header()` patches both fields in memory after capture.

If you hit a bare `HTTPError` traceback anywhere, the response body has the real
message — this code prints it instead of swallowing it.

## Personal voice vs professional voice

|                | Personal voice          | Professional (CNV Pro)              |
| -------------- | ----------------------- | ----------------------------------- |
| Input          | one clip, 5–90s         | 300+ utterances + transcripts       |
| Training       | seconds                 | ~10 compute hours, paid             |
| Output         | `speakerProfileId`      | a deployed endpoint                 |
| Access         | Limited Access form     | Limited Access form                 |
| Exportable     | no                      | no                                  |

This repo does personal voice. Enrollment is instant and the quality with
`DragonLatestNeural` is strong. Reach for Pro only if you need a specific brand
style or the HD conversational engine.

### You need approval first

Both tiers are Limited Access. Apply at <https://aka.ms/customneural>.

The gate is not where you'd expect, so don't assume you're approved because an
early call succeeds:

| Call                            | Before approval |
| ------------------------------- | --------------- |
| `PUT /projects`                 | 201 works       |
| `POST /consents` (upload+verify)| 201, verifies   |
| any `GET` (list/read)           | 200 works       |
| `POST /personalvoices`          | **403**         |

Only *creating the voice* is blocked. Everything leading up to it succeeds,
which makes it easy to believe you have access until the last step. `enroll`
caches your recording to `.prompt.wav` for exactly this reason — a 403 costs you
a retry, not another 60 seconds of talking.

## Using the voice from another service

```bash
curl -X POST "https://<region>.tts.speech.microsoft.com/cognitiveservices/v1" \
  -H "Ocp-Apim-Subscription-Key: <key>" \
  -H "Content-Type: application/ssml+xml" \
  -H "X-Microsoft-OutputFormat: audio-24khz-96kbitrate-mono-mp3" \
  -d "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis'
        xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'>
        <voice name='DragonLatestNeural'>
          <mstts:ttsembedding speakerProfileId='<your-spid>'/>
          <mstts:express-as style='Prompt'>Hello.</mstts:express-as>
        </voice>
      </speak>" --output out.mp3
```

## Responsible use

Clone only your own voice, or a voice you have explicit recorded permission to
use — that's what the consent step exists to enforce. Disclose synthetic speech
to listeners. Don't use this to impersonate anyone.

## License

MIT
