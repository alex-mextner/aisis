const dgram = require("node:dgram");
const g711 = require("g711");

const server = dgram.createSocket("udp4");

//server.on('message', (msg, rinfo) => {
//  // msg — raw RTP packet
//  console.log(`RTP packet from ${rinfo.address}:${rinfo.port} length ${msg.length}`);
//
//  // Здесь можно парсить RTP, например получить payload и декодировать G711
//  // Для примера просто выводим первое 12 байт RTP заголовка
//  console.log('RTP header:', msg.slice(0, 12));
//});

server.on("listening", () => {
  const address = server.address();
  console.log(`RTP listener on ${address.address}:${address.port}`);
});

server.on("message", (msg, rinfo) => {
  // RTP header 12 байт, далее payload
  const payload = msg.slice(12);

  // Пример: декодируем µ-law (ALAW использовать g711.alaw.decode)
  const pcmData = g711.ulaw.decode(payload);

  // pcmData - Buffer с 16-bit PCM сэмплами
  // Можно передавать на ASR или записывать

  console.log("Decoded PCM length:", pcmData.length);
});

const RTP_LISTEN_PORT = 10000;
server.bind(RTP_LISTEN_PORT);
