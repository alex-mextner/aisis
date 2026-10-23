import assert from 'node:assert/strict'
import test from 'node:test'
import { OpusEncoder } from '@discordjs/opus'
import { OpusProcessor } from './src/codecs/opus-processor'
import { OggParser } from './src/codecs/ogg-parser'

// Valid Ogg pages with CRC and actual CBR Opus packets. No decoder is mocked.
function page (sequence: number, lacing: number[], data: Buffer, flags = 0, granule = 0n) {
  assert.equal(lacing.reduce((a, b) => a + b, 0), data.length)
  const header = Buffer.alloc(27 + lacing.length)
  header.write('OggS'); header[5] = flags
  header.writeBigUInt64LE(granule, 6)
  header.writeUInt32LE(7, 14); header.writeUInt32LE(sequence, 18)
  header[26] = lacing.length
  Buffer.from(lacing).copy(header, 27)
  const result = Buffer.concat([header, data])
  let crc = 0
  for (const octet of result) {
    crc = (crc ^ (octet << 24)) >>> 0
    for (let i = 0; i < 8; i++) crc = ((crc & 0x80000000) ? (crc << 1) ^ 0x04c11db7 : crc << 1) >>> 0
  }
  result.writeUInt32LE(crc, 22)
  return result
}
function tags (vendor = 'aisis') {
  const text = Buffer.from(vendor)
  const size = Buffer.alloc(4); size.writeUInt32LE(text.length)
  return Buffer.concat([Buffer.from('OpusTags'), size, text, Buffer.alloc(4)])
}
function head (inputRate = 48000, gain = 0) {
  const data = Buffer.alloc(19)
  data.write('OpusHead'); data[8] = 1; data[9] = 1
  data.writeUInt32LE(inputRate, 12); data.writeInt16LE(gain, 16)
  return page(0, [19], data, 2)
}
function audio (bytes = 320) {
  const encoder = new OpusEncoder(48000, 1)
  encoder.setBitrate(bytes * 8 * 50)
  encoder.applyEncoderCTL(4006, 0) // OPUS_SET_VBR_REQUEST: valid fixed-length packet.
  const pcm = Buffer.alloc(1920)
  for (let i = 0; i < 960; i++) pcm.writeInt16LE(Math.round(Math.sin(i * .12) * 12000), i * 2)
  const packet = encoder.encode(pcm)
  assert.equal(packet.length, bytes)
  return packet
}
async function fixture (inputRate = 48000, gain = 0) {
  const chunks: Buffer[] = [], rates: number[] = []
  const processor = new OpusProcessor(async data => { chunks.push(data) }, async rate => { rates.push(rate) })
  await processor.handleAudioData(head(inputRate, gain))
  return { processor, chunks, rates }
}

test('valid Opus packet over255bytes is decoded once after its complete lacing chain', async () => {
  const f = await fixture(), packet = audio()
  const comment = tags()
  await f.processor.handleAudioData(page(1, [comment.length], comment))
  await f.processor.handleAudioData(page(2, [255, 65], packet, 4, 960n))
  assert.deepEqual(f.rates, [48000])
  assert.equal(f.chunks.length, 1)
  assert.equal(f.chunks[0].length, 1920)
})

test('packet continuation across Ogg pages never decodes an incomplete segment', async () => {
  const f = await fixture(), packet = audio(), comment = tags()
  await f.processor.handleAudioData(page(1, [comment.length], comment))
  await f.processor.handleAudioData(page(2, [255], packet.subarray(0, 255), 0, 0xffffffffffffffffn))
  assert.equal(f.chunks.length, 0)
  await f.processor.handleAudioData(page(3, [65], packet.subarray(255), 1 | 4, 960n))
  assert.equal(f.chunks.length, 1)
  assert.equal(f.chunks[0].length, 1920)
})

test('exactly255byte packets require and consume zero-length lacing terminator', async () => {
  const f = await fixture(), packet = audio(255), comment = tags()
  await f.processor.handleAudioData(page(1, [comment.length], comment))
  await f.processor.handleAudioData(page(2, [255], packet, 0, 0xffffffffffffffffn))
  assert.equal(f.chunks.length, 0)
  await f.processor.handleAudioData(page(3, [0], Buffer.alloc(0), 1 | 4, 960n))
  assert.equal(f.chunks.length, 1)
})

test('continued OpusTags ends its header page and later multi-packet audio preserves boundaries', async () => {
  const f = await fixture(), packet = audio(), comment = tags('v'.repeat(300))
  await f.processor.handleAudioData(page(1, [255], comment.subarray(0, 255), 0, 0xffffffffffffffffn))
  await f.processor.handleAudioData(page(2, [comment.length - 255], comment.subarray(255), 1, 0n))
  await f.processor.handleAudioData(page(3, [255, 65, 255, 65], Buffer.concat([packet, packet]), 4, 1920n))
  assert.equal(f.chunks.length, 2)
  assert.equal(f.chunks[0].length + f.chunks[1].length, 3840)
})

test('missing page in a continued packet fails before decoding damaged data', async () => {
  const f = await fixture(), packet = audio(), comment = tags()
  await f.processor.handleAudioData(page(1, [comment.length], comment))
  await f.processor.handleAudioData(page(2, [255], packet.subarray(0, 255), 0, 0xffffffffffffffffn))
  await assert.rejects(f.processor.handleAudioData(page(4, [65], packet.subarray(255), 1 | 4, 960n)), /sequence|missing/i)
  assert.equal(f.chunks.length, 0)
})

test('continued flag without its prior packet is rejected rather than guessed', async () => {
  const f = await fixture(), packet = audio(), comment = tags()
  await f.processor.handleAudioData(page(1, [comment.length], comment))
  await assert.rejects(f.processor.handleAudioData(page(2, [65], packet.subarray(255), 1 | 4, 960n)), /continu/i)
  assert.equal(f.chunks.length, 0)
})


test('truncated Ogg lacing or body is rejected before treating shortened data as a packet', async () => {
  const f = await fixture(), packet = audio(), comment = tags()
  await f.processor.handleAudioData(page(1, [comment.length], comment))
  const complete = page(2, [255, 65], packet, 4, 960n)
  assert.throws(() => OggParser.parse(complete.subarray(0, complete.length - 1)), /Truncated Ogg page body/)
  assert.equal(f.chunks.length, 0)
  assert.throws(() => OggParser.parse(complete.subarray(0, 28)), /Truncated Ogg lacing table/)
  assert.equal(f.chunks.length, 0)
})


test('informational OpusHead sample rate zero or44100 still decodes at supported48kHz', async () => {
  for (const inputRate of [0, 44100]) {
    const f = await fixture(inputRate), packet = audio(), comment = tags()
    await f.processor.handleAudioData(page(1, [comment.length], comment))
    await f.processor.handleAudioData(page(2, [255, 65], packet, 4, 960n))
    assert.deepEqual(f.rates, [48000])
    assert.equal(f.chunks[0].length, 1920)
  }
})

test('signed OpusHead output gain at offset16 is applied without mistaking it for mapping', async () => {
  const normal = await fixture(), quieter = await fixture(48000, -1536)
  const packet = audio(), comment = tags()
  for (const f of [normal, quieter]) {
    await f.processor.handleAudioData(page(1, [comment.length], comment))
    await f.processor.handleAudioData(page(2, [255, 65], packet, 4, 960n))
  }
  const peak = (data: Buffer) => {
    let result = 0
    for (let i = 0; i < data.length; i += 2) result = Math.max(result, Math.abs(data.readInt16LE(i)))
    return result
  }
  const ratio = peak(quieter.chunks[0]) / peak(normal.chunks[0])
  assert.ok(ratio > .49 && ratio < .52, `minus6dB gain ratio was${ratio}`)
})


test('transport frames can split Ogg header lacing and body without changing decoded PCM', async () => {
  const packet = audio(), comment = tags()
  const stream = Buffer.concat([head(), page(1, [comment.length], comment), page(2, [255, 65], packet, 4, 960n)])
  const decode = async (sizes: number[]) => {
    const chunks: Buffer[] = []
    const processor = new OpusProcessor(async data => { chunks.push(data) }, async () => {})
    let position = 0
    for (const size of sizes) {
      await processor.handleAudioData(stream.subarray(position, position + size))
      position += size
    }
    await processor.handleAudioData(stream.subarray(position))
    processor.finish()
    return Buffer.concat(chunks)
  }
  const expected = await decode([])
  assert.equal(expected.length, 1920)
  assert.deepEqual(await decode(Array(stream.length).fill(1)), expected)
  assert.deepEqual(await decode([3, 23, 1, 8, 37, 5, 100]), expected)
})

test('native EOF rejects a truncated transport page and an unfinished continued packet', async () => {
  const f = await fixture(), comment = tags(), packet = audio()
  const commentPage = page(1, [comment.length], comment)
  await f.processor.handleAudioData(commentPage.subarray(0, -1))
  assert.throws(() => f.processor.finish(), /truncated|incomplete/i)
  assert.equal(f.chunks.length, 0)
  const g = await fixture()
  await g.processor.handleAudioData(commentPage)
  await g.processor.handleAudioData(page(2, [255], packet.subarray(0, 255)))
  assert.throws(() => g.processor.finish(), /truncated|incomplete/i)
  assert.equal(g.chunks.length, 0)
})

test('fragmented malformed header is rejected as soon as its signature is available', async () => {
  const f = await fixture()
  await f.processor.handleAudioData(Buffer.from('Bad'))
  await assert.rejects(f.processor.handleAudioData(Buffer.from('!')), /Ogg/i)
})


test('oversized transport frame is rejected before buffering or decoding', async () => {
  const f = await fixture()
  await assert.rejects(f.processor.handleAudioData(Buffer.alloc(1024 * 1024 + 1)), /limit/i)
  assert.equal(f.chunks.length, 0)
})
