#!/usr/bin/env python3
"""
azure-voice-live — record from your mic and stream straight to Azure AI Speech.

  ./live.py enroll        record consent + voice prompt, stream to Azure,
                          get back a speakerProfileId (no files on disk)
  ./live.py say "text"    stream synthesized speech back and play it as it
                          arrives (playback starts before synthesis finishes)

Config comes from .env (see .env.example). Nothing is hardcoded.

Two things that cost me an hour, documented so they don't cost you one:

1. AUTH IS NOT UNIFORM.
     customvoice management API -> Authorization: Bearer <token>
     TTS synthesis endpoint     -> Authorization: Bearer aad#<resourceId>#<token>
   Plain bearer on the synthesis endpoint returns 401 with no useful message.

2. FFMPEG PIPES WRITE BROKEN WAV HEADERS.
   Writing WAV to stdout, ffmpeg can't seek back to fill in the RIFF/data
   size fields, so it emits 0xFFFFFFFF for both. Azure rejects the upload.
   fix_wav_header() patches them in memory after capture.
"""
import io, os, struct, subprocess, sys, uuid, json, time
import urllib.request, urllib.error


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

RES = os.environ.get("SPEECH_RESOURCE")
RG = os.environ.get("SPEECH_RESOURCE_GROUP")
REGION = os.environ.get("SPEECH_REGION", "eastus")
PROJECT = os.environ.get("VOICE_PROJECT", "my-voice")
TALENT = os.environ.get("VOICE_TALENT_NAME", "Your Name")
COMPANY = os.environ.get("VOICE_COMPANY_NAME", "Your Name")
MIC = os.environ.get("MIC", "0")
API = "2026-01-01"

if not RES or not RG:
    sys.exit("Set SPEECH_RESOURCE and SPEECH_RESOURCE_GROUP in .env (copy .env.example).")

EP = f"https://{RES}.cognitiveservices.azure.com"
CONSENT_ID = f"{PROJECT}-consent"
VOICE_ID = f"{PROJECT}-personal"


def sh(c):
    return subprocess.check_output(c, shell=True, text=True).strip()


TOK = sh("az account get-access-token --resource https://cognitiveservices.azure.com "
         "--query accessToken -o tsv")
RID = sh(f"az cognitiveservices account show -n {RES} -g {RG} --query id -o tsv")
TTSAUTH = f"aad#{RID}#{TOK}"   # composite form — see note 1 above


def fix_wav_header(w):
    """See note 2: repair the 0xFFFFFFFF sizes ffmpeg writes to a pipe."""
    w = bytearray(w)
    if w[:4] != b"RIFF":
        return bytes(w)
    struct.pack_into("<I", w, 4, len(w) - 8)
    i = w.find(b"data")
    if i > 0:
        struct.pack_into("<I", w, i + 4, len(w) - i - 8)
    return bytes(w)


def rec(seconds):
    """Capture mic -> 24kHz mono s16 WAV bytes, held in memory."""
    fmt = "avfoundation" if sys.platform == "darwin" else "alsa"
    src = f":{MIC}" if sys.platform == "darwin" else MIC
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", fmt, "-i", src,
           "-t", str(seconds), "-ac", "1", "-ar", "24000", "-sample_fmt", "s16",
           "-f", "wav", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True)
    if not p.stdout:
        sys.exit("ffmpeg produced no audio:\n" + p.stderr.decode()[:400])
    return fix_wav_header(p.stdout)


def multipart(fields, wav_bytes, filename="audio.wav"):
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"audiodata\"; "
              f"filename=\"{filename}\"\r\nContent-Type: audio/wav\r\n\r\n".encode())
    out.write(wav_bytes)
    out.write(f"\r\n--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"


def post_mgmt(path, body, ctype):
    req = urllib.request.Request(f"{EP}{path}?api-version={API}", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {TOK}", "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"\nHTTP {e.code} from {path}:\n{e.read().decode()[:1500]}\n")


def put_json(path, obj):
    req = urllib.request.Request(f"{EP}{path}?api-version={API}",
                                 data=json.dumps(obj).encode(), method="PUT",
                                 headers={"Authorization": f"Bearer {TOK}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (409,):      # already exists
            return {}
        sys.exit(f"\nHTTP {e.code} from {path}:\n{e.read().decode()[:1500]}\n")


def get_mgmt(path):
    req = urllib.request.Request(f"{EP}{path}?api-version={API}",
                                 headers={"Authorization": f"Bearer {TOK}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def poll(path, secs=300):
    for _ in range(secs // 3):
        d = get_mgmt(path)
        print(f"  {path.rsplit('/', 1)[-1]}: {d.get('status')}")
        if d.get("status") in ("Succeeded", "Failed"):
            return d
        time.sleep(3)
    return get_mgmt(path)


def enroll():
    put_json(f"/customvoice/projects/{PROJECT}",
             {"description": "personal voice", "kind": "PersonalVoice"})

    print("\nSTEP 1/2 — consent. Read this EXACTLY, in English (20s).")
    print("Azure speech-matches your audio against this script; a paraphrase fails.\n")
    print(f'  "I {TALENT} am aware that recordings of my voice will be used by')
    print(f'   {COMPANY} to create and use a synthetic version of my voice."\n')
    input("Enter to record consent...")
    wav = rec(20)
    print(f"  captured {len(wav)/1024:.0f} KB -> streaming to Azure")
    body, ct = multipart({"displayName": f"{TALENT} consent", "projectId": PROJECT,
                          "voiceTalentName": TALENT, "companyName": COMPANY,
                          "locale": "en-US"}, wav)
    post_mgmt(f"/customvoice/consents/{CONSENT_ID}", body, ct)
    d = poll(f"/customvoice/consents/{CONSENT_ID}")
    if d.get("status") != "Succeeded":
        print("\nconsent FAILED:", d.get("properties"))
        print("AudioAndScriptNotMatch just means re-read it more carefully. Nothing is broken.")
        sys.exit(1)

    print("\nSTEP 2/2 — voice prompt (60s). This defines what the clone sounds like.")
    print("Speak naturally and continuously. Any language, any content. Quiet room.\n")
    input("Enter to record prompt...")
    wav = rec(60)
    print(f"  captured {len(wav)/1024:.0f} KB -> streaming to Azure")
    body, ct = multipart({"projectId": PROJECT, "consentId": CONSENT_ID}, wav)
    post_mgmt(f"/customvoice/personalvoices/{VOICE_ID}", body, ct)
    d = poll(f"/customvoice/personalvoices/{VOICE_ID}")
    if d.get("speakerProfileId"):
        open(".spid", "w").write(d["speakerProfileId"])
        print(f"\nspeakerProfileId -> .spid\nNow: ./live.py say \"hello world\"")
    else:
        print(json.dumps(d, indent=2))


def say(text):
    spid = open(".spid").read().strip() if os.path.exists(".spid") else None
    if spid:
        voice = (f"<voice name='DragonLatestNeural'>"
                 f"<mstts:ttsembedding speakerProfileId='{spid}'/>"
                 f"<mstts:express-as style='Prompt'>{text}</mstts:express-as></voice>")
    else:
        print("(no .spid — run enroll first; using a stock voice for now)")
        voice = f"<voice name='en-US-AvaMultilingualNeural'>{text}</voice>"
    ssml = ("<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            "xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'>"
            f"{voice}</speak>").encode()

    req = urllib.request.Request(
        f"https://{REGION}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml, method="POST",
        headers={"Authorization": f"Bearer {TTSAUTH}",
                 "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
                 "User-Agent": "azure-voice-live"})
    t0 = time.time()
    player = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"],
                              stdin=subprocess.PIPE)
    n, first = 0, None
    try:
        with urllib.request.urlopen(req) as r:
            while chunk := r.read(4096):
                if first is None:
                    first = time.time() - t0
                    print(f"  first chunk at {first*1000:.0f} ms — playing while Azure synthesizes")
                n += len(chunk)
                player.stdin.write(chunk)
    except urllib.error.HTTPError as e:
        sys.exit(f"\nHTTP {e.code}:\n{e.read().decode()[:800]}\n")
    player.stdin.close()
    player.wait()
    print(f"  streamed {n/1024:.0f} KB in {time.time()-t0:.2f}s")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "enroll":
        enroll()
    elif cmd == "say":
        say(" ".join(sys.argv[2:]) or "Hello from my Azure personal voice.")
    else:
        print(__doc__)
