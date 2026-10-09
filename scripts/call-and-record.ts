#!/usr/bin/env bun
import Ari from "ari-client";
import { HfInference } from "@huggingface/inference";
import { readFileSync, existsSync } from "node:fs";
import { createNotionPage } from "./services/notion";
import { authorize, createCalendarEvent } from "./services/google";

const phoneNumber = String(process.env.PHONE_NUMBER); // номер для вызова
const appName = String(process.env.APP_NAME);

// Initialize Hugging Face client
const hf = new HfInference(process.env.HF_TOKEN);

// Function to transcribe audio using Whisper
async function transcribeAudio(audioFilePath: string): Promise<string> {
  if (!existsSync(audioFilePath)) {
    throw new Error(`Audio file not found: ${audioFilePath}`);
  }

  try {
    const audioBuffer = readFileSync(audioFilePath);
    const audioBlob = new Blob([audioBuffer], { type: "audio/wav" });
    const result = await hf.automaticSpeechRecognition({
      data: audioBlob,
      model: "openai/whisper-large-v3",
    });

    return result.text || "No transcription available";
  } catch (error) {
    console.error("Transcription error:", error);
    return "Transcription failed";
  }
}

const client = await Ari.connect("http://127.0.0.1:8088/ari", "node-ari-client", "ari-client-REDACTED-okay");

console.log("Successfully connected to ARI");
client.start(appName);

// Store recording file names for each channel
const channelRecordings = new Map<string, string>();

// Регистрируем обработчик старта канала в Stasis
client.on("StasisStart", async (event, channel) => {
  console.log(`Channel ${channel.id} entered Stasis`);

  // Определяем тип звонка по аргументам или контексту
  const isIncoming = event.args && event.args.includes("incoming");
  const callType = isIncoming ? "INCOMING" : "OUTGOING";

  console.log(`📞 ${callType} CALL - Channel: ${channel.id}`);

  // Для входящих звонков - отвечаем на звонок
  if (isIncoming) {
    try {
      await channel.answer();
      console.log(`✅ Answered incoming call on channel ${channel.id}`);
    } catch (error) {
      console.error(`❌ Failed to answer incoming call:`, error);
      return;
    }
  }

  // Воспроизводим гудок и начинаем запись для всех звонков
  setTimeout(async () => {
    const playback = await channel.play(
      {
        media: "sound:beep",
      },
      undefined as unknown as Ari.Playback,
    );
    if (!playback) {
      console.error("Error playing beep");
      return;
    }
    playback.once("PlaybackFinished", async () => {
      const recordFileName = `${callType.toLowerCase()}_recording_${Date.now()}`;
      channelRecordings.set(channel.id, recordFileName);
      await channel.record(
        {
          name: recordFileName,
          format: "wav",
          maxDurationSeconds: 3600,
          beep: false,
          ifExists: "overwrite",
        },
        undefined as unknown as Ari.LiveRecording,
      );
      console.log(`🎙️ Recording started: ${recordFileName} (${callType})`);
    });
  }, 1500);
});

// Запускаем звонок, который попадет в Stasis app
client.channels
  .originate({
    endpoint: `SIP/${phoneNumber}@my-sip-trunk`,
    app: appName,
    appArgs: "outgoing",
  })
  .then((channel) => {
    console.log(`Call originated, channel ID: ${channel.id}`);
    //setInterval(()=>console.log('*'), 3000);
  })
  .catch(console.error);

// Чтобы приложение не завершалось, поддерживаем event loop, например ждём on('StasisEnd') для канала
client.on("StasisEnd", async (_event, channel) => {
  console.log(`Channel ${channel.id} left Stasis`);

  // Get the recording file name for this channel
  const recordFileName = channelRecordings.get(channel.id);
  if (!recordFileName) {
    console.log(`No recording file name found for channel ${channel.id}`);
    return;
  }

  console.log(`Processing recording: ${recordFileName}`);

  // Wait a bit for the file to be fully written
  setTimeout(async () => {
    // Construct the full path to the recording file
    const recordingPath = `/var/spool/asterisk/recording/${recordFileName}.wav`;

    try {
      console.log(`Transcribing audio from: ${recordingPath}`);
      const transcription = await transcribeAudio(recordingPath);
      console.log("🎤 TRANSCRIPTION:");
      console.log("=".repeat(50));
      console.log(transcription);
      console.log("=".repeat(50));

      if (transcription !== "Transcription failed" && transcription !== "No transcription available") {
        const callDate = new Date().toISOString();
        const title = `Call with ${phoneNumber} on ${callDate}`;

        // Create Notion Page
        await createNotionPage(title, transcription);

        // Create Google Calendar Event
        const workAuth = await authorize("work");
        const eventStartTime = new Date();
        const eventEndTime = new Date(eventStartTime.getTime() + 30 * 60 * 1000); // 30 mins later
        await createCalendarEvent(
          workAuth,
          title,
          transcription,
          eventStartTime.toISOString(),
          eventEndTime.toISOString(),
        );
      }
    } catch (error) {
      console.error(`Failed to transcribe ${recordingPath}:`, error);
    }

    // Clean up the recording reference
    channelRecordings.delete(channel.id);
  }, 2000); // Wait 2 seconds for file to be fully written
});

// Просто не завершаем процесс, ожидаем Ctrl+C или програмно закрываем ARI при необходимости

const loop = () => setTimeout(loop, 1000);
