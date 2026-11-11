import assert from 'node:assert/strict'
import { once } from 'node:events'
import test from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { WebSocketServer } from 'ws'
import { AudioMetadataBackendSession, STTBackendSession } from './src/backend/backend'
import { GigaAMSTTBackend } from './src/backend/stt/gigaam'
import { VoiceInputHandler } from './src/routers/uniproxy/voice-input-handler'

function deferred<T = void> () {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function until (condition: () => boolean) {
  const deadline = Date.now() + 2000
  while (!condition()) {
    if (Date.now() >= deadline) throw new Error('behavior did not occur')
    await delay(1)
  }
}
class ASR extends STTBackendSession {
  closed = false
  finalSeen = false
  finishCalls = 0
  chunks: Buffer[] = []
  gate = deferred()
  async transcribeChunk (chunk: Buffer) { this.chunks.push(chunk) }
  async finish () {
    this.finishCalls += 1
    if (!this.finalSeen) await this.gate.promise
  }
  final (text: string) {
    this.finalSeen = true
    this.chunkTranscribed({ text, endOfUtt: true })
    this.gate.resolve()
  }
  partial (text: string) { this.chunkTranscribed({ text, endOfUtt: false }) }
  close () {
    this.closed = true
    if (!this.finalSeen) this.gate.reject(new Error('cancelled'))
  }
}
class Metadata extends AudioMetadataBackendSession {
  finishCalls = 0
  closed = false
  gate: ReturnType<typeof deferred<object>> | null = null
  async processChunk (_chunk: Buffer) {}
  async finish () { this.finishCalls += 1; return this.gate ? await this.gate.promise : {} }
  close () { this.closed = true }
}
async function fixture () {
  const asr = new ASR()
  // An ASR promise may be cancelled before a finish waiter exists.
  void asr.gate.promise.catch(() => {})
  const metadata = new Metadata()
  const handler = new VoiceInputHandler({
    stt: { startTranscribing: async () => asr },
    audioMetadata: { startCapturing: async () => metadata }
  })
  const finished: any[] = [], transcribed: any[] = [], errors: any[] = []
  handler.on('finish', event => finished.push(event))
  handler.on('transcribed', event => transcribed.push(event))
  handler.on('error', event => errors.push(event))
  await handler.handleVoiceInputEvent({})
  return { handler, asr, metadata, finished, transcribed, errors }
}

test('native EOF before VAD waits for the final result without closing STT', async () => {
  const f = await fixture()
  await f.handler.handleVoiceInputAudioEvent({ buffer: Buffer.from('accepted audio') })
  f.asr.partial('partial')
  const eof = f.handler.handleVoiceInputFinishEvent({})
  await until(() => f.asr.finishCalls === 1)
  assert.equal(f.asr.closed, false)
  assert.equal(f.finished.length, 0)
  assert.equal(f.asr.chunks.length, 1)
  f.asr.final('complete utterance')
  await eof
  assert.deepEqual(f.finished, [{ metadata: {}, text: 'complete utterance' }])
  assert.equal(f.asr.closed, true)
  assert.equal(f.errors.length, 0)
})

test('VAD and repeated native EOF converge to one finish and metadata collection', async () => {
  const f = await fixture()
  f.metadata.gate = deferred<object>()
  f.asr.final('one result')
  const native = f.handler.handleVoiceInputFinishEvent({})
  const duplicate = f.handler.handleVoiceInputFinishEvent({})
  await until(() => f.metadata.finishCalls === 1)
  assert.equal(f.finished.length, 0)
  f.metadata.gate.resolve({ origin: 'audio' })
  await Promise.all([native, duplicate])
  assert.equal(f.asr.finishCalls, 1)
  assert.deepEqual(f.finished, [{ metadata: { origin: 'audio' }, text: 'one result' }])
})

test('cancel while final recognition is pending produces no late transcription or finish', async () => {
  const f = await fixture()
  const eof = f.handler.handleVoiceInputFinishEvent({})
  await until(() => f.asr.finishCalls === 1)
  await f.handler.handleVoiceInputCancelEvent({})
  f.asr.final('must not escape cancelled session')
  await eof
  assert.deepEqual(f.finished, [])
  assert.deepEqual(f.transcribed, [])
  assert.deepEqual(f.errors, [])
})

test('cancel during metadata finalization suppresses the already recognized result', async () => {
  const f = await fixture()
  f.metadata.gate = deferred<object>()
  f.asr.final('recognized before cancel')
  await until(() => f.metadata.finishCalls === 1)
  await f.handler.handleVoiceInputCancelEvent({})
  f.metadata.gate.resolve({})
  await delay(5)
  assert.equal(f.finished.length, 0)
  assert.equal(f.errors.length, 0)
})

test('bounded finalization failure emits one error and never an empty finish', async () => {
  const f = await fixture()
  const eof = f.handler.handleVoiceInputFinishEvent({})
  await until(() => f.asr.finishCalls === 1)
  f.asr.gate.reject(new Error('recognition timed out'))
  await eof
  assert.equal(f.errors.length, 1)
  assert.equal(f.finished.length, 0)
  assert.equal(f.asr.closed, true)
})

test('cancellation before STT startup finishes closes late resources', async () => {
  const ready = deferred<STTBackendSession>()
  const asr = new ASR(); void asr.gate.promise.catch(() => {})
  const metadata = new Metadata()
  const handler = new VoiceInputHandler({ stt: { startTranscribing: () => ready.promise }, audioMetadata: { startCapturing: async () => metadata } })
  const finished: any[] = []
  handler.on('finish', event => finished.push(event))
  const starting = handler.handleVoiceInputEvent({})
  await handler.handleVoiceInputCancelEvent({})
  ready.resolve(asr)
  await starting
  asr.final('late startup response')
  assert.equal(asr.closed, true)
  assert.equal(metadata.closed, true)
  assert.deepEqual(finished, [])
})

async function socketFixture (reply: boolean) {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await once(server, 'listening')
  let controls = 0
  server.on('connection', client => client.on('message', (raw, binary) => {
    assert.equal(binary, false)
    assert.deepEqual(JSON.parse(raw.toString()), { type: 'finish' })
    controls += 1
    if (reply) setTimeout(() => client.send(JSON.stringify({ text: 'wire final', end_of_utt: true })), 10)
  }))
  const session = await new GigaAMSTTBackend(`ws://127.0.0.1:${(server.address() as any).port}`).startTranscribing()
  return { session, controls: () => controls, vadFinal: () => { for (const client of server.clients) client.send(JSON.stringify({ text: 'VAD final', end_of_utt: true })) }, close: async () => { session.close(); server.close(); await once(server, 'close') } }
}

test('actual GigaAM websocket finish sends one EOF and waits for final JSON', async () => {
  const f = await socketFixture(true)
  const results: any[] = []
  f.session.setCallback(value => results.push(value))
  try {
    await Promise.all([f.session.finish(), f.session.finish()])
    assert.equal(f.controls(), 1)
    assert.deepEqual(results, [{ text: 'wire final', endOfUtt: true }])
    await f.session.finish()
    assert.equal(f.controls(), 1)
  } finally { await f.close() }
})

test('actual GigaAM websocket cancel rejects final wait instead of emitting a result', async () => {
  const f = await socketFixture(false)
  const results: any[] = []
  f.session.setCallback(value => results.push(value))
  try {
    const waiting = assert.rejects(f.session.finish(), /cancelled/)
    await until(() => f.controls() === 1)
    f.session.close()
    await waiting
    assert.deepEqual(results, [])
  } finally { await f.close() }
})

test('actual GigaAM native EOF rejects an incomplete Ogg transport page before ASR finish', async () => {
  const f = await socketFixture(false)
  try {
    await f.session.transcribeChunk(Buffer.from('OggS'))
    await assert.rejects(f.session.finish(), /Truncated Ogg page/)
    assert.equal(f.controls(), 0)
  } finally { await f.close() }
})


test('actual GigaAM VAD final wins over a partial next Ogg page without sending native EOF', async () => {
  const f = await socketFixture(false)
  const results: any[] = []
  f.session.setCallback(value => results.push(value))
  try {
    // The next transport page is incomplete when the recognizer ends speech.
    await f.session.transcribeChunk(Buffer.from('OggS'))
    f.vadFinal()
    await until(() => results.length === 1)
    await Promise.all([f.session.finish(), f.session.finish()])
    assert.deepEqual(results, [{ text: 'VAD final', endOfUtt: true }])
    assert.equal(f.controls(), 0)
  } finally { await f.close() }
})
