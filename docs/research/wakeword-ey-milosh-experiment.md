# Статус: кастомная wake word модель `ey_milosh`

## 0. Поправка 2026-09-24: модель не готова, июньские метрики недействительны

- **Wake word выключен** (порог `1.0` в `ha.docker-compose.yaml`, коммит 62ed44a).
  В проде `ey_milosh` дала 1010 срабатываний за 9 дней записи с микрофона
  (100–250 в сутки, в основном 19–23 ч при работающем ТВ). Реальных позитивов
  с голосом пользователя ноль.
- **Вывод «222 ч шума, 0.0 FA/h» ниже неверен.** Все файлы
  `raw_live/2026-06-*` — цифровая тишина (peak 0): запись шла не с того
  устройства. Оценщик 10 млн раз выдал один и тот же скор 0.0197
  (mean = max), этого никто не проверил.
- Реальный шум без фразы есть только за 24–26.08 (~50 ч). Всё, на что
  модель там срабатывает, — ложные срабатывания (hard negatives), а не
  позитивы.
- Вживую wake-сервис получает поток после WebRTC NS=3/AGC=15 из
  wyoming-satellite; модель обучалась на чистом синтетическом голосе одного
  Piper-диктора (`irina`).
- Новая честная оценка: `~/dev/wakeword/wakebench.py` (воспроизводит
  логику wyoming-openwakeword 2.1.0, опционально фронтенд satellite,
  гейты: тишина, константный выход, replay против логов прода).
- Дальнейшая работа: aisis spec 140 (alex-mextner/aisis#3) и задачи в
  vad-cli / stt-cli. Разделы ниже сохранены как история.

Дата обновления: 2026-06-20

## 1. Текущий статус

Это не финально готовая wake word модель, потому что еще нет реальных positive-записей голоса пользователя. Но текущий live-trial кандидат уже совместим с реальным `pyopen_wakeword` runtime в `rhasspy/wyoming-openwakeword` и прошел full streaming negative evaluation на недельной записи домашнего шума.

Что еще не сделано:

- нет 50-100 реальных positive-записей голоса пользователя
- нет размеченного live trial набора true wake / false wake
- нет финального отчета по реальному recall на голосе пользователя

Текущий live-trial candidate:

- Активный файл: `/home/ultra/oww-models/ey_milosh.tflite`
- Размер: 1849152 bytes
- SHA256: `9464893f1d25b9a1c6873a83145e43527b58998a1da3601abba7dffd62861cf8`
- TFLite input shape: `[1, 16, 96]` (`pyopen_wakeword` deployment layout)
- Порог в `ha.docker-compose.yaml`: `0.991`
- Сервис: `wyoming-openwakeword`
- Wake word name в satellite: `ey_milosh`
- Backup V1: `/home/ultra/oww-models/backups/ey_milosh_v1_backup_20260601_130310.tflite`
- Backup previous active V2: `/home/ultra/oww-models/ey_milosh.tflite.backup.20260602-190941`
- Backup incompatible training-layout active file: `/home/ultra/oww-models/ey_milosh.tflite.backup.20260612-pre-pyopen-layout`
- PyOpen-compatible iter7 artifact: `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/dnn/ey_milosh.pyopen.tflite`

Критическое исправление 2026-06-20:

- root cause для "то ли не слышит, то ли не показывает" найден: HA
  `shell_command` создавал каталог positive session как `root:root`, а
  `wyoming-satellite` работает как `1000:1000` и не мог писать WAV в
  `/dataset/positives/real_user/<session>/`
- уже накопившиеся root-owned каталоги `positive_sessions` и
  `positives/real_user` переведены в `ultra:ultra` без `sudo`, через
  одноразовый `docker run --rm -v /home/ultra/oww-dataset:/dataset alpine ...`
- `scripts/wakeword_real_positive_session.py` теперь при старте сессии
  выставляет `capture_uid/capture_gid` и режимы доступа для session JSON,
  summary JSON и output dir, чтобы satellite мог писать напрямую
- `scripts/wakeword_channel0_tee.py` больше не сохраняет training clip, если
  речь не закончилась до `3.0s`; такой фрагмент отбрасывается как
  `continued_past_max`
- минимальная длительность training clip и mined candidate поднята до `1.0s`,
  максимальная - `3.0s`
- `scripts/wakeword_smoke_manifest.py mine-fragments` теперь:
  - добавляет стабильный `id`, `sample_time`, `sample_time_epoch`
  - сортирует candidates от новых к старым
  - помечает `training_mode` по active/summary sessions и `positives/real_user`
  - включает консервативный music-filter
  - пишет review flags `pending/confirmed/negative`
- текущий review manifest пересобран:
  - candidates: `200`
  - duration range: `1.0..2.97s`
  - sort: newest-first
  - music_filter: `enabled`
- удаление в review UI заменено на добавление в noise negatives:
  `scripts/wakeword_review_candidate.py negative` копирует WAV в
  `/home/ultra/oww-dataset/negatives/review_rejected/`
- зеленая галочка `confirm` копирует WAV в
  `/home/ultra/oww-dataset/positives/review_confirmed/`; только эти
  подтвержденные review samples должны использоваться как real positives в
  следующем обучении
- review state хранится в обычном
  `/home/ultra/homeassistant/www/wakeword/review-status.json`, не в `.storage`
- Studio UI показывает дату/время sample, training-mode badge,
  confirmed/negative badges, последовательный playlist с паузой `500ms`,
  кнопки `Play`, `Подтвердить`, `Добавить в шумовые negative samples`
- кнопка "Завершить" теперь только останавливает session; она больше не
  двигает план и не помечает блок выполненным
- UI проигрывает короткий browser-click и мигает счетчиком, когда
  `captured_files_since_start` реально увеличился
- live direction отображается из ReSpeaker: основной угол из
  `sensor.wakeword_mic_direction`, confidence/age fallback из
  `sensor.wakeword_mic_level`
- проверено:
  - `ha_validate_yaml.sh packages/wakeword_voice_collection.yaml configuration.yaml`
  - `ha_validate_lovelace.sh`
  - `node --check www/wakeword/wakeword-collection-card.js`
  - `python3 -m py_compile` для wakeword scripts
  - HA API: новые `shell_command.wakeword_review_candidate_confirm` и
    `shell_command.wakeword_review_candidate_negative` загружены
  - `agent-browser` Studio snapshot: custom-card открыт, direction live,
    review list показывает `200` candidates и новые action buttons

Критическое исправление 2026-06-19:

- `wyoming-satellite` pass-through capture после завершенной weekly noise session
  мог уходить в старый `/app/sounds/capture_ch0.sh`, где был hardcoded
  `arecord -D plughw:2,0`
- сейчас `plughw:2,0` соответствует `sof-hda-dsp`, а ReSpeaker USB Mic Array
  виден как `plughw:CARD=ArrayUAC10,DEV=0`
- это объясняет отсутствие новых `*-wake.wav`: сегодня сессия
  `20260619T150436Z` дала `copied_wake_files=0` при `expected_attempts=30`
- capture wrapper теперь стабильно использует `plughw:CARD=ArrayUAC10,DEV=0`
  и не проваливается в старый capture script после завершения raw-live записи
- `sensor.wakeword_mic_direction` читает `DOAANGLE`/`VOICEACTIVITY` из
  ReSpeaker USB tuning endpoint `2886:0018`
- `sensor.wakeword_mic_level` читает live status из 6-channel ReSpeaker stream,
  который одновременно кормит Wyoming satellite
- UI Studio больше не использует browser microphone для качества записи; в
  snapshot проверено: направление отображается как ReSpeaker array, review
  manifest видит `120` фрагментов и `35` missed candidates
- activation/done WAV playback проверен через ReSpeaker:
  `aplay -D plughw:CARD=ArrayUAC10,DEV=0 /app/sounds/awake.wav`
- positive collection больше не зависит от успешного wake trigger:
  `scripts/wakeword_channel0_tee.py` следит за
  `/dataset/positive_sessions/current.json` и пишет utterance WAV напрямую из
  ReSpeaker/Wyoming stream в `/dataset/positives/real_user/<session>/`
- HA session report теперь отдельно считает:
  - `captured_files_since_start` - сколько обучающих WAV уже записано
  - `wake_files_since_start` - сколько раз реально сработала активная модель
  - `capture_recall` - доля записанных попыток
  - `observed_trigger_recall` - доля wake-срабатываний активной модели
- synthetic recorder smoke-test прошел: активная test-session, тишина, tone,
  тишина -> ровно один WAV clip и JSON sidecar

Критическое исправление 2026-06-12:

- предыдущий активный TFLite имел input shape `[1, 96, 16]`
- `pyopen_wakeword` трактует вторую размерность как число временных окон, поэтому для `[1, 96, 16]` ожидал 96 окон при внутреннем буфере 80 и не выдавал model scores в live runtime
- модель переложена в deployment layout `[1, 16, 96]`; первая dense-матрица транспонирована так, чтобы выход совпадал со старой моделью
- equivalence check old-vs-new: `max_abs_diff=2.3841858e-07`
- `wyoming-openwakeword` перезапущен, HA container не перезапускался

После замены 2026-06-12 перезапущен только wake service:

```bash
docker compose -f ha.docker-compose.yaml restart wyoming-openwakeword
```

Проверено по логам:

- `wyoming-openwakeword` нашел `/custom-models/ey_milosh.tflite`
- threshold загружен как `0.991`
- model list: `['ey_milosh']`
- satellite client подключился к wake service
- Home Assistant container не перезапускался
- continuous raw recorder подключен к тому же channel 0 mic stream через `wakeword_capture_ch0_record.sh`
- live noise dataset пишется в `/home/ultra/oww-dataset/raw_live/YYYY-MM-DD/HHMMSS.wav`
- weekly background-noise recording session started: `/home/ultra/oww-dataset/raw_live_session.json`
- weekly background-noise recording completed: `2026-06-09T17:45:01Z`
- final collected noise duration: `222.0614h` across `13327` raw live WAV files
- watchdog cron runs every 15 minutes and sends HA notifications on completion/stall:
  `*/15 * * * * cd /home/ultra/homeassistant && scripts/wakeword_noise_watchdog.py check >> /tmp/wakeword_noise_watchdog.log 2>&1`
- HA notification path tested: `persistent_notification.create` and `notify.notify` both returned OK
- reboot persistence checked:
  - Docker service is enabled and running
  - cron service is enabled and running
  - `wyoming-satellite` and `wyoming-openwakeword` have `restart=unless-stopped`
- interruption test passed: after `docker compose -f ha.docker-compose.yaml restart wyoming-satellite`, recorder resumed with a new segment and previous WAV files remained readable
- recorder resilience:
  - capture wrapper loops forever and restarts the `arecord | tee` pipeline after failures
  - WAV segments are 60 seconds
  - WAV headers are refreshed and fsynced every 5 seconds, so abrupt interruption should lose at most the unsynced tail of the current segment, not prior segments
- post-completion behavior:
  - after `completion_notification_sent_at` is set in `/home/ultra/oww-dataset/raw_live_session.json`, the satellite mic wrapper falls back to pass-through capture and stops appending to `raw_live/`
  - set `WAKEWORD_RECORD_AFTER_COMPLETE=1` only if another explicit noise-recording session is needed
  - pass-through mode verified 2026-06-12: `wyoming-satellite` logs `completed session found ... without raw_live recording`
- full raw-live streaming evaluation completed 2026-06-13:
  - shard reports: `/home/ultra/oww-models/stream_eval/raw_live_2026-06-*.json`
  - merged report: `/home/ultra/oww-models/stream_eval/raw_live_merged.json`
  - processed: `13327/13327` WAV files, `222.2163h`
  - runtime: `pyopen_wakeword`, thresholds `0.97,0.99,0.991,0.992,0.995,0.999`, refractory `2s` and `5s`
  - max score: `0.01971021667122841`
  - threshold crossings: `0` for every tested threshold
  - detections: `0` for every tested threshold/refractory pair
  - measured background-noise FPR: `0.0/h` at active threshold `0.991`

Offline report по этому кандидату:

- `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/dnn/metrics_tflite_stt1200.json`
- `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/run_report.json`

Fresh active TFLite audit, 2026-06-19:

- report: `/home/ultra/oww-models/active_eval/ey_milosh_active_20260619_tflite_stt1200.json`
- command: `docker run --rm --user 1000:1000 ... oww-train:latest python3 /workspace/homeassistant/scripts/wakeword_iterate.py --max-stt-windows 1200 evaluate-tflite --model /workspace/models/ey_milosh.tflite --output /workspace/models/active_eval/ey_milosh_active_20260619_tflite_stt1200.json`
- validation selected threshold: `0.99`, validation recall `0.9942`,
  validation FP `0`
- deployed threshold `0.991` on test sweep:
  `TP=658`, `FN=10`, `FP=0`, `TN=1615`, recall `0.9850`,
  measured offline FPR `0.0/h`
- threshold `0.99` on the same test sweep has the same recall `0.9850`, but
  introduces `FP=1`, so deployment stays at `0.991`
- threshold `0.998` keeps `FP=0`, but recall drops to `0.9760`; this is too
  conservative while real-user positives are not collected yet
- test negative max score: `0.990655779838562`, which is just below the active
  `0.991` threshold

Real-data collection report:

```bash
python3 scripts/wakeword_dataset_report.py --json
```

## 2. Что изменилось в методике

Старый статус был слишком оптимистичным: он опирался на random window split и не отделял модельную метрику от реального deployment-порога.

Новая методика:

1. Split делается по файлам, а не по окнам, чтобы окна одного WAV не попадали одновременно в train и validation.
2. Короткие synthetic positives паддятся до полного openWakeWord context window.
3. V2 использует `CONTEXT_FRAMES=16` (`~1.99s`) вместо V1 `CONTEXT_FRAMES=8` (`~1.35s`).
4. Валидация считает threshold sweep и выбирает более высокий порог среди вариантов с допустимым FPR, если recall почти не падает.
5. Отдельно считаются:
   - validation recall/FPR
   - test recall/FPR на holdout split
   - adversarial negatives
   - home false wake negatives
   - sampled STT debug negatives
6. Baseline и live-trial candidate проверяются именно как `.tflite`, а не только как PyTorch checkpoint.

Новый инструмент:

- `scripts/wakeword_iterate.py` - dataset discovery, feature extraction, training, threshold sweep, reports.
  - Added inline STT hard-negative mining: score debug STT candidate windows with a TFLite model, exclude overlap with deterministic STT test windows, and add only high-score windows as train negatives.
  - Threshold sweep now includes high-resolution `0.991..0.999` points for near-saturated models.
- `scripts/wakeword_torch_to_tflite.py` - прямой конвертер DNN checkpoint -> Keras/TFLite.
- `scripts/wakeword_patch_tflite_layout.py` - patches old `[1,96,context]` DNN TFLite into pyopen-compatible `[1,context,96]` without TensorFlow.
- `scripts/wakeword_stream_eval.py` - pyopen runtime streaming false-positive evaluator for long raw WAV directories.
- `scripts/wakeword_merge_stream_eval.py` - merges sharded streaming-eval JSON reports.
- `scripts/wakeword_wait_merge_stream_eval.py` - waits for shard reports and writes merged report.
- `scripts/wakeword_capture_ch0_record.sh` + `scripts/wakeword_channel0_tee.py` - live recorder: keeps feeding satellite while writing real mono WAV segments.
- `scripts/wakeword_dataset_report.py` - reports raw live hours, debug recordings, curated real positives, and false wakes.
- `scripts/wakeword_real_positive_session.py` - controlled real-user positive collection from Wyoming `*-wake.wav` files.
- `scripts/wakeword_noise_watchdog.py` - tracks the 168 recorded-hour noise session and notifies HA when complete or stalled.
- `scripts/install_wakeword_noise_cron.sh` - idempotently installs the current-user watchdog cron.
- `scripts/wakeword_smoke_manifest.py` - dependency-light offline smoke train/eval plus short voice-like fragment mining and UI-ready manifest output.

### Smoke train/eval и manifest, 2026-06-19

Добавлен отдельный smoke-инструмент для ситуации, когда нужно быстро получить
хоть какие-то wake-срабатывания без сети, TensorFlow, `pyopen_wakeword` и
training container. Это не production openWakeWord-модель и не замена активного
`/home/ultra/oww-models/ey_milosh.tflite`; это быстрый локальный прототипный
скорер на stdlib+numpy для triage и UI/review manifest.

Команда полного smoke-прогона:

```bash
scripts/wakeword_smoke_manifest.py all \
  --model-output /home/ultra/oww-models/smoke/ey_milosh_smoke_model.json \
  --report /home/ultra/oww-models/smoke/smoke_train_eval_report.json \
  --audio-root /home/ultra/wyoming-debug \
  --output /home/ultra/oww-models/smoke/short_wake_fragments_manifest.json \
  --clip-root /home/ultra/oww-models/smoke/short_wake_fragments \
  --max-files 80 \
  --max-candidates 120
```

Текущий smoke-прогон:

- модель: `/home/ultra/oww-models/smoke/ey_milosh_smoke_model.json`
- train/eval report: `/home/ultra/oww-models/smoke/smoke_train_eval_report.json`
- выбранный smoke threshold: `0.4`
- test positives detected: `62/64`
- short-fragment manifest: `/home/ultra/oww-models/smoke/short_wake_fragments_manifest.json`
- clips: `/home/ultra/oww-models/smoke/short_wake_fragments/`
- scan slice: `80` newest WAV files from `/home/ultra/wyoming-debug`
- candidates: `120`
- `detected_by_model`: `85`
- `missed_by_model`: `35`

Manifest rows contain at least:

- `path`
- `duration`
- `score`
- `energy`
- `detected_by_model`
- `missed_by_model`

Additional fields currently include `source_path`, `start_seconds`, and
`end_seconds`, so a reviewer can map each clip back to the original debug WAV.

## 3. Важное открытие по данным

Каталог `/home/ultra/oww-dataset/raw/` нельзя считать 7k часами аудио.

Фактически найдено:

- `raw_empty_or_header_only`: 43602 WAV-файла
- почти все эти файлы имеют размер 44 bytes, то есть это только WAV header без аудио
- такие файлы исключены из обучения и метрик

Реально использованные данные:

| Source | Files | Hours / windows | Role |
|---|---:|---:|---|
| Synthetic positives | 4500 | 0.8469 h | train/val/test |
| Adversarial negatives | 2500 | 0.5856 h | train/val/test |
| Home false wake negatives | 216 | 0.1200 h | train/val/test |
| Debug STT recordings | 78 | 82.85 h available, 1200 deterministic sampled windows in current deploy audit | test only |

## 4. Итерации и метрики

Все цифры ниже получены через `scripts/wakeword_iterate.py` на seed `20260601`.

### Baseline: V1 TFLite

Файл: `/home/ultra/oww-models/ey_milosh_v1/ey_milosh.tflite`

| Metric | Value |
|---|---:|
| Context | 8 frames |
| Selected threshold | 0.99 |
| Validation recall | 0.000 |
| Validation FPR/hour | 0.000 |
| Test recall | 0.000 |
| Test FPR/hour | 15.835 |

Вывод: V1 практически непригодна. На безопасном пороге она не слышит positives, а на низких порогах дает сотни ложных срабатываний в час по validation estimate.

Report: `/home/ultra/oww-models/metrics_baseline_v1.json`

### Iteration 1: V2 DNN/GRU

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter1/`

| Model | Threshold | Validation recall | Validation FPR/h | Test recall | Test FPR/h | Notes |
|---|---:|---:|---:|---:|---:|---|
| DNN16 | 0.62 | 0.999 | 0.000 | 0.987 | 3.028 | 1 FP on adversarial `Милос` |
| GRU16 | 0.27 | 1.000 | 0.000 | 1.000 | 0.000 | Best metric, not deployed yet |

Вывод: GRU выглядит лучше, но DNN проще и уже конвертируется в TFLite. Для DNN методика выбора порога была улучшена, потому что порог 0.62 давал слабый запас против hard negatives.

### Iteration 2: V2 DNN with robust threshold

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter2_dnn_margin/`

Финальный TFLite:

- `/home/ultra/oww-models/ey_milosh_v2_iter2_dnn_margin/dnn/ey_milosh.tflite`
- Скопирован в `/home/ultra/oww-models/ey_milosh.tflite`

TFLite verification:

| Metric | Validation | Test |
|---|---:|---:|
| Threshold | 0.97 | 0.97 |
| Recall | 0.9928 | 0.9850 |
| Precision | 1.0000 | 1.0000 |
| FPR/hour | 0.0000 | 0.0000 |
| False positives | 0 / 425 negatives | 0 / 815 negatives |
| False negatives | 5 / 694 positives | 10 / 668 positives |

Report: `/home/ultra/oww-models/ey_milosh_v2_iter2_dnn_margin/dnn/metrics_tflite.json`

Вывод: V2 DNN TFLite достаточно хороша для live trial. Главная цена порога `0.97` - часть single-word positives `Милош` теряется. Фраза `эй Милош` на synthetic holdout прошла без FN.

### Iteration 3: DNN recall recovery, larger STT sample

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter3_dnn_recall/`

Параметры:

- `--max-stt-windows 1200`
- `--epochs 60`
- `--architectures dnn`
- `--context-frames 16`
- `--hidden 192`
- `--negative-weight 2.0`
- `--positive-augmentations 4`

PyTorch checkpoint metrics:

| Metric | Validation | Test |
|---|---:|---:|
| Selected threshold | 0.81 | 0.81 |
| Recall | 0.9914 | 0.9835 |
| FP / FPR/hour | 0 / 0.0000 | 0 / 0.0000 |
| Debug STT sampled windows | - | 1200 |
| Debug STT FP | - | 0 |
| Debug STT score max | - | 0.4866 |

Artifacts:

- `/home/ultra/oww-models/ey_milosh_v2_iter3_dnn_recall/dnn/ey_milosh_v2_iter3_dnn_recall_dnn.pt`
- `/home/ultra/oww-models/ey_milosh_v2_iter3_dnn_recall/dnn/ey_milosh_v2_iter3_dnn_recall_dnn.onnx`
- `/home/ultra/oww-models/ey_milosh_v2_iter3_dnn_recall/dnn/metrics.json`
- `/home/ultra/oww-models/ey_milosh_v2_iter3_dnn_recall/run_report.json`

Conversion blocker: `scripts/wakeword_torch_to_tflite.py` requires TensorFlow, but `oww-train:latest` and local `hga/` do not currently have TensorFlow installed. No TFLite was produced for this run.

Вывод: iter3 is safer on the enlarged STT sample than active iter2 TFLite, but recall regressed. Not a deploy candidate unless TFLite conversion is solved and the lower recall tradeoff is explicitly accepted.

### Iteration 4: DNN recall recovery, lower negative weight

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter4_dnn_recall_nw16/`

Параметры совпадают с iter3, except `--negative-weight 1.6`.

PyTorch checkpoint metrics:

| Metric | Validation | Test |
|---|---:|---:|
| Selected threshold | 0.97 | 0.97 |
| Recall | 0.9957 | 0.9835 |
| FP / FPR/hour | 0 / 0.0000 | 3 / 3.8839 |
| Debug STT sampled windows | - | 1200 |
| Debug STT FP | - | 3 |
| Debug STT score max | - | 0.9996 |

Artifacts:

- `/home/ultra/oww-models/ey_milosh_v2_iter4_dnn_recall_nw16/dnn/ey_milosh_v2_iter4_dnn_recall_nw16_dnn.pt`
- `/home/ultra/oww-models/ey_milosh_v2_iter4_dnn_recall_nw16/dnn/ey_milosh_v2_iter4_dnn_recall_nw16_dnn.onnx`
- `/home/ultra/oww-models/ey_milosh_v2_iter4_dnn_recall_nw16/dnn/metrics.json`
- `/home/ultra/oww-models/ey_milosh_v2_iter4_dnn_recall_nw16/run_report.json`

Вывод: iter4 is not a deploy candidate. It improves validation recall vs iter3, but test recall remains below active iter2 and STT false positives return.

### Active iter2 TFLite re-check on 1200 STT windows

Report: `/home/ultra/oww-models/ey_milosh_v2_iter2_dnn_margin/dnn/metrics_tflite_stt1200.json`

| Metric | Value |
|---|---:|
| Threshold | 0.97 |
| Validation recall | 0.9928 |
| Validation FP / FPR/hour | 0 / 0.0000 |
| Test recall | 0.9850 |
| Test FP / FPR/hour | 3 / 3.8839 |
| Debug STT sampled windows | 1200 |
| Debug STT FP | 3 |
| Debug STT score max | 0.9977 |

This shows the previous `debug STT max score 0.8609` and FP=0 result was sample-size dependent (`max-stt-windows 400`). On the larger deterministic 1200-window sample, active iter2 has good recall, but it no longer has offline FP=0.

High-threshold re-check:

Report: `/home/ultra/oww-models/ey_milosh_v2_iter2_dnn_margin/dnn/metrics_tflite_stt1200_highgrid.json`

| Threshold | Test recall | Test FP / FPR/hour | Notes |
|---:|---:|---:|---|
| 0.97 | 0.9850 | 3 / 3.8839 | Active deployment threshold |
| 0.998 | 0.9581 | 0 / 0.0000 | Best zero-FP active-model threshold |

Вывод: простой подъем threshold у active iter2 убирает FP только ценой сильного recall regression.

### Iteration 5: DNN HNM, filled hard-negative batch

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter5_dnn_hnm_nw18/`

Параметры:

- mined from active `/home/ultra/oww-models/ey_milosh.tflite`
- `--hard-negative-candidates 8000`
- `--hard-negative-windows 256`
- `--hard-negative-score-floor 0.60`
- `--hidden 192`
- `--negative-weight 1.8`
- `--positive-augmentations 5`

PyTorch checkpoint metrics:

| Metric | Validation | Test |
|---|---:|---:|
| Selected threshold | 0.97 | 0.97 |
| Recall | 0.9914 | 0.9790 |
| FP / FPR/hour | 0 / 0.0000 | 1 / 1.2946 |
| Best zero-FP audit threshold | - | 0.98 |
| Best zero-FP audit recall | - | 0.9775 |

Mining report: 7819 candidates after overlap filter; 256 selected, but only 19 were above score floor. This diluted hard-negative training with easy STT negatives.

Вывод: not a deploy candidate. It reduced STT pressure but hurt recall.

### Iteration 6: DNN strict HNM, lower negative weight

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter6_dnn_hnm_strict_nw16/`

Параметры:

- strict hard-negative floor; no fill below floor
- `--hard-negative-candidates 16000`
- `--hard-negative-windows 128`
- `--hard-negative-score-floor 0.50`
- `--hidden 192`
- `--negative-weight 1.6`
- `--positive-augmentations 5`

PyTorch checkpoint metrics:

| Metric | Validation | Test |
|---|---:|---:|
| Selected threshold | 0.92 | 0.92 |
| Recall | 0.9928 | 0.9805 |
| FP / FPR/hour | 0 / 0.0000 | 0 / 0.0000 |
| Best zero-FP audit threshold | - | 0.89 |
| Best zero-FP audit recall | - | 0.9820 |

Mining report: 15703 candidates after overlap filter; 39 selected, all above score floor; selected score range `0.5066..0.9997`.

Вывод: HNM works for FP suppression, but recall is below active iter2 and iter3.

### Iteration 7: DNN strict HNM, recall recovery

Run dir: `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/`

Параметры:

- strict hard-negative floor; no fill below floor
- `--hard-negative-candidates 16000`
- `--hard-negative-windows 128`
- `--hard-negative-score-floor 0.50`
- `--hidden 256`
- `--epochs 70`
- `--negative-weight 1.2`
- `--positive-augmentations 6`

Mining report:

- 15703 candidates after overlap filter
- 39 selected hard negatives
- selected score range `0.5066..0.9997`
- selected median score `0.9484`

PyTorch checkpoint metrics:

| Metric | Validation | Test |
|---|---:|---:|
| Selected threshold | 0.99 | 0.99 |
| Recall | 0.9942 | 0.9850 |
| FP / FPR/hour | 0 / 0.0000 | 1 / 1.2946 |
| Fine audit threshold | - | 0.991 |
| Fine audit recall | - | 0.9850 |
| Fine audit FP / FPR/hour | - | 0 / 0.0000 |

TFLite verification:

Report: `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/dnn/metrics_tflite_stt1200.json`

| Threshold | Test recall | Test FP / FPR/hour | Test FN | Negative max |
|---:|---:|---:|---:|---:|
| 0.99 | 0.9850 | 1 / 1.2946 | 10 | 0.9907 |
| 0.991 | 0.9850 | 0 / 0.0000 | 10 | 0.9907 |

Artifacts:

- `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/dnn/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256_dnn.pt`
- `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/dnn/ey_milosh.tflite`
- `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/dnn/metrics_tflite_stt1200.json`
- `/home/ultra/oww-models/ey_milosh_v2_iter7_dnn_hnm_strict_nw12_h256/run_report.json`

Вывод: promoted to live-trial candidate 2026-06-02, not final readiness. It matches previous active iter2 synthetic/STT-window test recall (`0.9850`) while removing the 3 STT false positives on the deterministic 1200-window sample. Active `/home/ultra/oww-models/ey_milosh.tflite` now points to this iter7 TFLite artifact, and `wyoming-openwakeword` runs it with threshold `0.991`.

## 5. Ограничения текущих метрик

Метрики стали честнее, но это еще не финальная оценка.

Ограничения:

1. Нет реальных positive-записей пользователя. Recall сейчас измерен на synthetic Piper voices.
2. STT/debug negatives в training audit все еще взяты sampled windows; full streaming audit сделан отдельно только по `raw_live/`.
3. Full `raw_live/` streaming evaluation показал `0.0` false wakes/hour, но это negative-only фоновый набор. Он не заменяет real-positive recall тест.
4. GRU16 показал лучшие offline-метрики, но пока не конвертирован и не проверен как TFLite.

## 6. Следующий цикл

Приоритеты следующей итерации:

1. Live trial 24-48 часов на активной V2 DNN HNM:
   - считать новые `*-wake.wav` в `/home/ultra/wyoming-debug/`
   - вручную пометить true wake / false wake
   - добавить false wakes в hard negatives
2. Записать 50-100 реальных positives:
   - `эй Милош`
   - `Милош`
   - разные расстояния, шум ТВ, обычная речь
   - controlled session:
     ```bash
     python3 scripts/wakeword_real_positive_session.py start --phrase "эй Милош" --expected-attempts 50
     # Say the phrase 50 times near the satellite.
     python3 scripts/wakeword_real_positive_session.py finish
     ```
3. Разметить live trial wake/debug записи:
   - true wake
   - false wake
   - missed wake
4. Проверить GRU16 TFLite feasibility:
   - если Keras GRU conversion пройдет и pyopen_wakeword примет модель, сравнить live с DNN.

## 7. Команды воспроизведения

Baseline V1:

```bash
docker run --rm --user 1000:1000 \
  -v /home/ultra/homeassistant:/workspace/homeassistant:ro \
  -v /home/ultra/oww-dataset:/workspace/dataset:ro \
  -v /home/ultra/oww-models:/workspace/models \
  -v /home/ultra/wyoming-debug:/workspace/wyoming-debug:ro \
  -v /home/ultra/openWakeWord:/workspace/openWakeWord:ro \
  oww-train:latest \
  python3 /workspace/homeassistant/scripts/wakeword_iterate.py \
    --max-stt-windows 400 \
    evaluate-tflite \
    --model /workspace/models/ey_milosh_v1/ey_milosh.tflite \
    --output /workspace/models/metrics_baseline_v1.json
```

Final DNN iteration:

```bash
docker run --rm --user 1000:1000 \
  -v /home/ultra/homeassistant:/workspace/homeassistant \
  -v /home/ultra/oww-dataset:/workspace/dataset:ro \
  -v /home/ultra/oww-models:/workspace/models \
  -v /home/ultra/wyoming-debug:/workspace/wyoming-debug:ro \
  -v /home/ultra/openWakeWord:/workspace/openWakeWord:ro \
  oww-train:latest \
  python3 /workspace/homeassistant/scripts/wakeword_iterate.py \
    --max-stt-windows 400 \
    --selection-recall-tolerance 0.005 \
    iterate \
    --run-name ey_milosh_v2_iter2_dnn_margin \
    --epochs 45 \
    --architectures dnn \
    --context-frames 16 \
    --negative-weight 2.5 \
    --positive-augmentations 2
```
