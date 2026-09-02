# Cliente de prueba local (no va en la imagen Docker): abre el micrófono del
# host y lo transmite al servicio por WebSocket. Ver README para el uso.

import asyncio
import sys

import numpy as np
import sounddevice as sd
import websockets

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8000/ws/transcribe"


async def main():
    audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def callback(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        pcm16 = (np.clip(indata[:, 0] * 32768, -32768, 32767)).astype(np.int16)
        loop.call_soon_threadsafe(audio_queue.put_nowait, pcm16.tobytes())

    async with websockets.connect(URL) as ws:
        print(f"Conectado a {URL}. Hablá por el micrófono (Ctrl+C para salir)...\n")

        async def sender():
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=FRAME_SIZE,
                callback=callback,
            ):
                while True:
                    chunk = await audio_queue.get()
                    await ws.send(chunk)

        async def receiver():
            async for message in ws:
                import json

                data = json.loads(message)
                prefix = "✅" if data["type"] == "final" else "🎙️ "
                end = "\n" if data["type"] == "final" else ""
                print(f"\r{prefix} {data['text']}" + " " * 20, end=end, flush=True)

        await asyncio.gather(sender(), receiver())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nFinalizando...")
