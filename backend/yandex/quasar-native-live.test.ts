// Synthetic integration probe only: microphone hardware and ChatGPT are not used.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { AudioMetadataBackendSession } from './src/backend/backend'
import { GigaAMSTTBackend } from './src/backend/stt/gigaam'
import { VoiceInputHandler } from './src/routers/uniproxy/voice-input-handler'
class Metadata extends AudioMetadataBackendSession {
  close () {}
  async finish () { return {} }
  async processChunk (_chunk: Buffer) {}
}
test('real Ogg Opus -> native VoiceInputHandler EOF -> Wyoming final exactly once', { timeout: 45000 }, async () => {
  const handler = new VoiceInputHandler({
    audioMetadata: { startCapturing: async () => new Metadata() },
    stt: new GigaAMSTTBackend('ws://127.0.0.1:31121')
  })
  const finished: any[] = [], errors: any[] = []
  handler.on('finish', value => finished.push(value))
  handler.on('error', value => errors.push(value))
  try {
    await handler.handleVoiceInputEvent({})
    await handler.handleVoiceInputAudioEvent({ buffer: readFileSync('/aisis-probe.ogg') })
    // Intentionally do not pad silence: this is the native stream-close path.
    await handler.handleVoiceInputFinishEvent({})
    assert.equal(errors.length, 0)
    assert.equal(finished.length, 1)
    const normalized = finished[0].text.toLowerCase().replace(/[^а-яёa-z0-9 ]/g, '').trim()
    assert.equal(normalized, 'расскажи почему осенью листья меняют цвет')
    await handler.handleVoiceInputFinishEvent({})
    assert.equal(finished.length, 1)
    console.log(JSON.stringify({ native_eof: 'ok', codec: 'Ogg Opus', asr: normalized, finish_events: finished.length }))
  } finally { handler.close() }
})
