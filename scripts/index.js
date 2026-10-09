//const Ari = require('ari-client');
//
//Ari.connect('http://127.0.01:8088/ari', 'node-ari-client', 'ari-client-REDACTED-okay')
//    .then(client => {
//        console.log('Successfully connected to ARI');
//
//        // Подписка на события StasisStart
//        client.on('StasisStart', event => {
//            console.log('Channel entered Stasis:', event.channel.name);
//
//            // Автоматически отвечаем на входящий вызов
//            client.channels.answer({ channelId: event.channel.id })
//                .then(() => {
//                    console.log('Answered the call');
//                })
//                .catch(err => {
//                    console.error('Error answering the call:', err);
//                });
//        });
//
//        // Запуск приложения ARI
//        client.start('my_ari_app');
//    })
//    .catch(err => {
//        console.error('Error connecting to ARI:', err);
//    });

const Ari = require("ari-client");
Ari.connect("http://127.0.0.1:8088/ari", "node-ari-client", "ari-client-REDACTED-okay")
  .then((client) => {
    console.log("Successfully connected to ARI");
    return client.channels.originate({
      endpoint: "SIP/my-sip-trunk/375291346026", // Звоним на внешний номер
      extension: "100",
      context: "outgoing", //'default',
      priority: 1,
      app: "my_ari_app", // Приложение ARI, которое получит вызов
    });
  })
  .catch(console.error);
