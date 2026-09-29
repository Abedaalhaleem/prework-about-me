# Pose model compatibility (capability D)

**Status: DISABLED.** Reviewed 2026-09-29.

This document records what was checked before deciding whether RoomSense can offer
any Wi-Fi human-pose output. The result drives
`backend/roomsense/inference/pose/gate.py`, `configs/pose_model_manifest.json` and
`configs/pose_requirements.json`.

## 1. Conclusion

No compatible model with usable weights exists for this project's hardware, so
capability D stays **DISABLED**.

* The peer-reviewed Wi-Fi pose systems (DensePose From WiFi, Person-in-WiFi,
  Person-in-WiFi 3D, the MM-Fi Wi-Fi modality) were trained on Intel 5300 or Atheros
  NICs with **3 receive antennas per receiver** and 30 or 114 subcarriers. The papers
  were read for DensePose From WiFi and Person-in-WiFi 3D; the other two come from
  search summaries only. Both papers that were read use **phase sanitised across
  antennas**. No trained weights were found for any of the four: DensePose From WiFi
  has no official code, Person-in-WiFi released only data-collection tools, the
  Person-in-WiFi 3D repository has no checkpoint, and MM-Fi is a dataset.
* RF-Pose / RF-Pose3D is an **FMCW radar with antenna arrays**. It is not Wi-Fi
  communication hardware, and no router or ESP32 can run it.
* The popular community "wifi-densepose" code was checked line by line. In the
  revisions checked, its parser **fills CSI with random numbers**, the model-weight
  loading is **commented out**, and the pose outputs are **random keypoints**
  (details in §4.6). The current version of that project reports an ESP32 checkpoint
  trained on one recording, with a self-reported PCK@20 of 3.0%.
* The community ESP32 pose projects found use 2 to 9 synchronised links (RoomSense
  has one link per board) or a 56-subcarrier input. They report only same-recording
  or random-split numbers. None publishes independently validated weights in a safe
  format with a known licence.

A DensePose *image* model cannot take CSI as input. A radar model cannot run on a
router. A multi-antenna model cannot be fed single-antenna data by copying channels.
The gate's exact-match requirements rule out all three.

## 2. What RoomSense hardware provides

Declared by this project (`docs/ARCHITECTURE.md`, `backend/roomsense/csi_layouts.py`).
These values are not measured by this document.

| Property | Value |
|---|---|
| Boards | ESP32 / -S2 / -S3 / -C3 ("classic") receivers, one USB serial link each |
| Antennas | 1 TX antenna → 1 RX antenna per link. No antenna array. |
| CSI segment | LLTF, 64 positions, **52 occupied subcarriers** (k = −26…26 without DC) with `lltf_only` |
| Values | int8 imaginary/real pairs; the pipeline uses **amplitude only** (`processing.use_phase = false`) |
| Phase | **Not available.** Separate boards share no RF clock, so there is no phase synchronisation, and the pipeline does not process phase. |
| Packet rate | Configurable 1–100 Hz in the firmware (`RS_RATE_HZ`, default 25 Hz). The gate compares against the **measured** rate. |
| Links | As many as the user wires up. Each one is an independent 1×1 link. |

`runtime_hardware_profile()` in `gate.py` converts a frame's `layout_id` into these
values. For a classic LLTF link it gives `csi_source="esp32-classic-lltf"`, 1×1
antennas, 52 subcarriers, and `phase_available=False`.

## 3. Compatibility matrix

"Reported" means stated by the source and not reproduced here. "Not verified" means
the primary source could not be read from this environment (see §8).

| Work | Radio hardware | TX×RX antennas | Subcarriers | Rate | Phase used | Code | Weights | Licence | Independent test | Fits RoomSense? |
|---|---|---|---|---|---|---|---|---|---|---|
| DensePose From WiFi (Geng, Huang, De la Torre; arXiv 2301.00250) | Dataset reused from Person-in-WiFi [31]. The paper names TP-Link AC1750 only as an *example* router with 3 antennas. | 3×3 | 30 (2.4 GHz ± 20 MHz) | 100 Hz | Yes (sanitised) | No official code found | None found | Not verified | No: random 80/20 frame split ("same layout"); one held-out layout reported separately | **No** (3×3, 30 subcarriers, phase) |
| Person-in-WiFi (ICCV 2019) | Intel 5300, Linux 802.11n CSI Tool (collection tools in repo; the rest not verified) | 3 RX antennas (per search summary; not verified) | 30 (not verified) | Not verified | Not verified | Only data-collection tools ("due to IRB issues, we have not made code publicly") | None | Tools repo: MIT | Not verified | **No** |
| Person-in-WiFi 3D (CVPR 2024) | 4 × ThinkPad X201 with Intel 5300. 1 TX, 3 RX. | 1 TX antenna; 3 RX × 3 antennas | 30 on ch. 128 (5.64 GHz) | 300 pkt/s | Yes (denoised) | Yes (Apache-2.0) | None in repo; none linked | Code Apache-2.0; project/dataset CC BY-NC 4.0 | No: per clip, first 550 frames train and last 50 test (same people, rooms, sessions) | **No** |
| MM-Fi (NeurIPS 2023), Wi-Fi modality | TP-Link N750 + Atheros CSI Tool (search summary; not verified) | 1 TX, 3 RX antennas (not verified) | 114 at 5 GHz / 40 MHz (not verified) | ~100 Hz after on-device averaging (not verified) | Toolbox loader reads `CSIamp` only | Toolbox only | None (dataset, not a model) | No LICENSE file in toolbox repo; dataset licence not verified | Offers cross-subject and cross-environment splits besides a random split | **No** |
| RF-Pose / RF-Pose3D (MIT CSAIL) | FMCW radar with vertical and horizontal antenna arrays, ~5.4–7.2 GHz sweep (search summary; not verified) | Antenna arrays | n/a (FMCW, not OFDM CSI) | Not verified | n/a | No official repository found | None found | Not verified | Not verified | **No**, and not Wi-Fi at all |
| ruvnet/wifi-densepose (2025-06 revision 5101504) and copy davidakpele/wifi-densepose | Claims ESP32 / routers | n/a | Parser emits random arrays | n/a | n/a | Yes (MIT) | None; `torch.load` commented out | MIT | No evaluation code | **No**: outputs are random |
| ruvnet/wifi-densepose "RuView" (HEAD 5ef001b, 2026-09) `pose_v1` | ESP32 | Not stated in files read | 56-subcarrier input (Conv1d 56→…) | 20-frame windows; rate not stated | No | Yes (MIT) | `pose_v1.safetensors` (507 KB) | MIT | No: temporal 80/20 split of one 30-min recording, 216 eval samples. Self-reported PCK@20 = 3.0%. | **No** (56 ≠ 52 subcarriers, not validated) |
| sel00000/csi-pose | 6 × ESP32-S3 (3 TX × 3 RX = 9 links), ESP-NOW | 1×1 per link, 9 links | 56 (HT20) | ~103 pkt/s | No (amplitude only) | Yes (MIT) | None in repo | MIT | No: single session, one subject, one room, time-ordered 80/20. Self-reported PCK@0.2 = 0.495. | **No** (9 links, 56 subcarriers, no weights) |
| gavmc/CSI | 4 × ESP32-WROOM-32 + Netgear X6 AP | 1×1 per link, 4 receivers | 52 (LLTF) | ~50 Hz | No | Yes | `model.pt` (pickle-based, not loaded) | No LICENSE file | No. The author says the PCK@0.1 ≈ 0.63 is inflated by overlapping random-split windows. | **No** (4 links, pickle, no licence, no valid test) |
| Abinand2631/Wifi-Vision | 1 × ESP32 TX, 2 × RX | 1×1 per link, 2 receivers | Not stated in files read | Not stated in files read | Not stated | Yes | `tednet_3d_best.pth` (pickle-based, not loaded) | No LICENSE file | No split or metric figures found in the README | **No** |

## 4. Findings per source

### 4.1 DensePose From WiFi (Geng, Huang, De la Torre; CMU; arXiv 2301.00250)

arXiv is blocked from this environment. The paper was read from a PDF copy committed
in `superstar1225/DensePose_from_WiFi` @ `1cedb239943e080810ebc5655c5d2ed22ad02d7c`
(file `DensePose From WiFi.pdf`, sha256 `79e410d6…ff69195`). The copy carries the arXiv
stamp `arXiv:2301.00250v1 [cs.CV] 31 Dec 2022`. We could not check whether later arXiv
versions differ.

* **Hardware and input.** "The raw CSI data are sampled in 100Hz as complex values over
  30 subcarrier frequencies (linearly spaced within 2.4GHz±20MHz) transmitting among 3
  emitter antennas and 3 reception antennas". The input is "a 150×3×3 amplitude tensor
  and a 150×3×3 phase tensor" (5 consecutive samples × 30 frequencies). The introduction
  mentions "many WiFi routers, such as TP-Link AC1750, come with 3 antennas", but only as
  a cost argument. The paper does not say the dataset was recorded with that router.
* **Data.** Reuses the dataset of Person-in-WiFi [31]: 16 spatial layouts (6 in a lab
  office, 10 in a classroom), about 13 minutes each, 1–5 subjects, 8 subjects in total.
  Video was recorded at 20 FPS. There are **no manual annotations**. Pseudo ground truth
  comes from detectron2 DensePose `R_101_FPN_s1x_legacy` and Keypoint R-CNN `R101-FPN`
  run on the RGB frames.
* **Preprocessing.** Phase unwrapping, then filtering and linear fitting (§3.1). A
  modality translation network maps CSI to a 3×720×1280 feature map that feeds a
  DensePose-RCNN.
* **Protocols and results (reported).** In the "same layout" protocol, 80% of samples
  are selected at random for training, and train and test share "the same person's
  identities and background". Results: AP 43.5, AP@50 87.2, dpAP·GPS 45.3. In the
  "different layout" protocol (train on 15 layouts, test on 1 unseen classroom layout),
  AP falls to 27.3 and dpAP·GPS to 25.4. There is **no person-independent or
  day-independent test**.
* **Compute (reported).** 4 × Titan X, batch 16. About 80 hours from random
  initialisation; transfer learning shortens this.
* **Code and weights.** The paper has no code or weight link. No official repository was
  found in GitHub searches. Community reimplementations were checked:
  `superstar1225/DensePose_from_WiFi` feeds `np.random.rand(150, 3, 3)`
  (`Modality_Translation_Network.ipynb`, cell source lines 11–12) and contains no
  weights. `barleyjohn/DensePose-From-WiFi` @ `671196768b0b` is a notebook copy with no
  data loading. `xyz38324/DensePose-from-WiFi` @ `33eafa45e6fa` expects an MM-Fi-style
  directory layout and ships no weights or LICENSE file.
* **Fit.** No: 3×3 antennas, 30 subcarriers, phase, and no weights.

### 4.2 Person-in-WiFi (Wang, Zhou, Panev, Han, Huang; ICCV 2019)

`geekfeiw/wifiperson` @ `e0d091c5ba1784e61add18ffebbb1ca4ba3e60bc` (MIT) says: *"due
to IRB issues, we have not made code publicly. Still, we release data collection
tools"*. The tools are `datacollectioncode/wifiwithtimestamp/log_to_file_time.c` (a
patch for the Linux 802.11n CSI Tool, which targets Intel 5300) and a webcam
timestamp recorder. Annotations came from a camera through Mask R-CNN and OpenPose. The
paper PDF was blocked (CVF, CMU and arXiv hosts). A search summary says "Intel 5300 …
30 EM waves … 20 MHz centring at 2.4 GHz" and 3 receive antennas 12.5 cm apart; we
could not verify this. **No model code, weights or dataset link was found. Fit: no.**

### 4.3 Person-in-WiFi 3D (Yan, Wang, Qian, Ding, Han, Wei; CVPR 2024)

The paper was read from the PDF in the project-page repository
`aiotgroup/Person-in-WiFi-3D` @ `5fc6c5fe8e53e3316a022c47f9de844216f706a4`
(`paper/2024CVPR_Person_in_WiFi_3D.pdf`, fetched via raw.githubusercontent.com). The
code was read from `aiotgroup/Person-in-WiFi-3D-repo` @
`468aff35f0b042671b88bd22dc19428e5152f593`.

* **Hardware (paper §3).** "four ThinkPad X201 laptops, one as a transmitter and the
  remaining three as receivers, all equipped with Intel 5300 network cards … channel
  128 (5.64GHz) with 30 subcarriers and one antenna at a rate of 300 packets per
  second. The three receivers … each utilizing three antennas." An Azure Kinect records
  RGB-D at 15 FPS and was "manually synchronize[d]". Each sample is 1×3×3×30×20
  (TX, RX, antenna, subcarrier, time).
* **Preprocessing (code).** `opera/datasets/wifi_pose.py`: `CSI_sanitization` (lines
  138–171) removes a linear phase ramp using the **three antennas of each receiver
  jointly**, and `dwt_amp` applies a db11 wavelet to the amplitude. Amplitude and phase
  are concatenated. 14 keypoints.
* **Data and split (paper §5.1).** 7 volunteers, 3 locations (office, classroom,
  corridor), 456 clips of 40 s. Per clip, "first 550 for training, last 50 for
  testing", which gives 89,946 training and 7,824 test samples. This is a
  **within-clip temporal split**: the test set shares people, rooms and sessions with
  training. The ground truth comes from the Kinect Body Tracking SDK, and frames where
  the SDK failed were discarded.
* **Results (reported).** 3D joint error of 91.7 mm (1 person), 108.1 mm (2 people) and
  125.3 mm (3 people). Reported training cost is about 20 min per epoch at minibatch
  64, stated in a comparison run on one Nvidia 3090.
* **Weights and licences.** The config sets `load_from = None`. The repo contains no
  checkpoint files, and the README and project page link datasets only (Baidu, sdp8,
  Kaggle), with no weights. The code README says Apache-2.0 (copyright Hikvision
  Research Institute, from the Opera toolbox). The live licence statement on the project
  page is **CC BY-NC 4.0**; a CC BY 4.0 statement is in an HTML comment.
* **Fit.** No: 3 receivers × 3 antennas, 30 subcarriers at 5.64 GHz, phase across
  antennas, and no weights.

### 4.4 MM-Fi (Yang et al., NeurIPS 2023 Datasets and Benchmarks), Wi-Fi modality

Toolbox `ybhbingo/MMFi_dataset` @ `c9c1e3a8d5099779a618eae1bb351e253daa52d0`:

* The dataset has 40 subjects, 4 environments (E01–E04) and 27 actions. Raw RGB/IR
  images are withheld for privacy; 17 keypoints extracted from the images are provided
  instead.
* `mmfi_lib/mmfi.py` lines 232–245 load `CSIamp` from `frame*.mat`, fill NaN/inf
  values with column means, and min-max normalise each frame. The loop runs over 10
  entries of the third axis. The loader reads amplitude only.
* `config.yaml` defines `random_split` (ratio 0.8, seed 0), `cross_scene_split` (train
  E01–E03, test E04) and `cross_subject_split`.
* There is **no LICENSE file** in the toolbox repo. The dataset licence is not verified,
  because the paper, OpenReview and the project page were blocked.
* Hardware, from a search summary of arXiv 2305.10345 (**not verified**): two TP-Link
  N750 APs with the Atheros CSI Tool, 5 GHz, 40 MHz, 114 subcarriers per antenna pair,
  up to 1000 Hz, averaged in firmware to about 100 Hz, 1 TX antenna and 3 RX antennas.
* **Fit.** No: it is a dataset, not a model, and its hardware is not ESP32. It is a
  useful *benchmark* for method development, but its numbers say nothing about ESP32
  CSI.

### 4.5 RF-Pose (CVPR 2018) and RF-Pose3D (SIGCOMM 2018), MIT CSAIL

rfpose.csail.mit.edu, rfpose3d.csail.mit.edu, the CVF and DSpace PDFs, and the authors'
pages were all blocked. From search summaries (**not verified**): RF-Pose3D uses "an
FMCW radio equipped with a vertical and horizontal antenna arrays", sweeping about
5.4–7.2 GHz. The DensePose From WiFi paper (§2, read) makes the same distinction:
RF-Pose-type systems "do not work under the IEEE 802.11n/ac WiFi communication
standard … They rely on additional high-frequency and high-bandwidth electromagnetic
fields, which need specialized technology". GitHub searches found no official code or
weights. This is **radar research**. It must not be presented as something a router or
an ESP32 can do.

### 4.6 Community "wifi-densepose" repositories

`ruvnet/wifi-densepose` (the GitHub page now titles it RuView) was cloned with full
history. The code was inspected at commit `5101504b72afc868a1be17bd6060651d21436251`
(2025-06-09):

| File:line | What it does |
|---|---|
| `src/hardware/csi_extractor.py:81–84` | ESP32 parser: "simplified for now" → `amplitude = np.random.rand(num_antennas, num_subcarriers)`, `phase = np.random.rand(...)` |
| `src/hardware/csi_extractor.py:129–135` | Atheros parser "placeholder implementation" → `np.random.rand(3, 56)` |
| `src/hardware/csi_extractor.py:326` | `_read_raw_data` returns the hard-coded bytes `b"CSI_DATA:1234567890,3,56,…"` |
| `src/core/csi_processor.py:390` | `doppler_shift = np.random.rand(10)  # Placeholder` |
| `src/services/pose_service.py:105–106` | `torch.load(...)` and `load_state_dict(...)` **commented out**; `DensePoseHead()` keeps random initial weights |
| `src/services/pose_service.py:297–343` | Poses from `random.randint` / `random.uniform` (person count, confidence, keypoint x/y, activity) |
| `README.md:1018–1020` | Claims "Pose Detection Accuracy: 94.2%", "Fall Detection Sensitivity: 96.5%" with no evaluation code behind them |

`davidakpele/wifi-densepose` @ `0d72e033ef4b5a9427641291d84113dd125069bf` (2026-09-06)
still contains the same code: `src/hardware/csi_extractor.py:83–84, 134–135`,
`src/core/csi_processor.py:390`, `src/services/pose_service.py:105–106` and `:316` (a
random `_generate_keypoints`). Its README advertises "through walls" tracking.

The audit fork `deletexiumu/wifi-densepose` @ `30071bfe…` documents these findings. Its
own `v1/` snapshot is a *later* revision in which the ESP32 parser raises an error
instead of returning random data, so the line numbers in its README only match the
older upstream revision above.

The current upstream HEAD `5ef001b4fef3e08433043ebd248e6121f1221528` (2026-09-27)
reports on itself in `docs/adr/ADR-187-archive-v1-deprecation-honest-labeling.md`. It
says `archive/v1`'s `DensePoseHead` is random-initialised and has no checkpoint. It also
says a committed `v2/crates/cog-pose-estimation/cog/artifacts/pose_v1.safetensors` was
trained on "a single 30-min seated-at-desk recording (1,077 samples)" with
`encoder_init: random`. `train_results.json` gives `pck_at_20 = 2.968`,
`pck_at_50 = 18.52` on a `temporal_80_20` split with 216 evaluation samples, and the
input is Conv1d over 56 subcarriers × 20 frames. The ADR also cites Hugging Face
checkpoints (`ruvnet/wifi-densepose-pretrained`, `ruvnet/wifi-densepose-mmfi-pose`,
82.69% torso-PCK@20 on MM-Fi `random_split`). huggingface.co was blocked, so these are
**not verified**. A random split is not an independent test in any case.

**Fit: no.** The early revisions produce random output. The current single-recording
model is not independently validated and expects 56 subcarriers, not 52.

### 4.7 ESP32-specific pose work

No peer-reviewed ESP32 pose paper with released weights was found; searches only
turned up community projects. Each was cloned and its weight files listed. Pickle-based
checkpoints were **not loaded**.

* `sel00000/csi-pose` @ `02b1001590beea8e7d6daac57a4d8b4a345eb50f` (MIT). It ports
  WiSPPN to 6 ESP32-S3 boards (3 TX × 3 RX = 9 links). ESP-NOW runs at about 103 pkt/s,
  with HT20 and 56 subcarriers, amplitude only. The input is (5 packets × 56) × 3 × 3 =
  280 × 3 × 3, and an RTMPose webcam teacher provides labels. The README calls its
  results "Single session, single subject, single room (time-ordered 80/20 split)" and
  reports PCK@0.2 = 0.495 (self-reported). It shows the most careful synchronisation
  found (lower-envelope clock fit, about ±15 ms residual). **No weights are in the
  repo.**
* `gavmc/CSI` @ `b3130e0278964effa3d20bcc90a0a392a41b8cfa` (no LICENSE file). It uses
  4 ESP32-WROOM-32 receivers and a Netgear X6 AP at about 50 Hz. It uses LLTF with 52
  subcarriers, the same segment as RoomSense, in `[4, 50, 52]` windows, with YOLO-pose
  labels. The README says: "That PCK number is inflated … adjacent windows share 80% of
  their frames". `v2/server/model.pt` is a pickle-based checkpoint. The status is
  "Paused".
* `Abinand2631/Wifi-Vision` @ `5dacdc9683d9cd233ee1933480ad42d002b46583` (no LICENSE
  file). It uses 1 TX and 2 RX ESP32 boards. `models/tednet_3d_best.pth` is a
  pickle-based checkpoint. The README says "Low MSE and MAE" on a self-collected dataset
  and gives no split or figures.

**Fit: no.** Each one needs 2 to 9 synchronised links, and none has an independent
evaluation. None publishes weights in a safe format with a known licence.

## 5. Things the gate refuses on principle

* **Reshaping data to fit a model.** A model trained on 3×3 antennas and 30 or 114
  subcarriers is not fed 1×1 × 52 data by padding, tiling, copying channels or
  interpolating. `csi_layouts.py` already refuses to pad or truncate; the gate requires
  exact counts (R09–R12).
* **Treating an image model as a CSI model.** DensePose (detectron2) consumes RGB images.
  In DensePose From WiFi the image model was only the *teacher* on synchronised camera
  frames, and a separately trained network consumed CSI.
* **Relabelling radar as Wi-Fi.** FMCW radar with antenna arrays measures time-of-flight
  and angle. An ESP32 or a router cannot.
* **Accepting published numbers as local evidence.** Metrics copied from a paper do not
  transfer to another room, wall or radio (R16).
* **Accepting leaky splits.** Random-frame and within-clip splits share people, sessions
  and often overlapping windows between train and test (R15).
* **Loading pickle.** `.pt`, `.pth`, `.pkl`, `.joblib` and `.ckpt` files can run code
  when loaded (R06). Only safetensors or ONNX can be declared, and this release ships no
  runtime for either.

## 6. Exact missing requirements for this hardware

Ids refer to `configs/pose_requirements.json` and `gate.py`.

| Id | Missing |
|---|---|
| R02 | No model is installed (`installed: false`). |
| R03, R05, R06 | No weights exist with a known licence, a safe format and a hash. No inference runtime is shipped. |
| R07–R12 | Every model reviewed differs in at least one of: CSI source and packet format, antennas per receiver (3), number of synchronised links (2–9), or subcarrier count (30, 56 or 114 instead of 52). |
| R13 | The peer-reviewed setups sample at 100 Hz (DensePose From WiFi) or 300 packets/s (Person-in-WiFi 3D). The RoomSense firmware can be configured for 1–100 Hz (default 25 Hz). A model's rate must match the measured rate within its tolerance. |
| R14 | DensePose From WiFi and Person-in-WiFi 3D require phase that is sanitised across antennas. RoomSense has no usable phase. |
| R15 | No reviewed evaluation separates test data by session, person **and** day. |
| R16 | Nothing has been measured with the user's boards in the user's room. |
| R17 | No evaluation dataset for this hardware exists. |

## 7. A reproducible path, if someone wants to try

This is a research plan, not a promise that it would work. Nothing in it is enabled by
this release. Every step is local.

1. **Hardware.** Pick one configuration and freeze it. That means chip, firmware
   version, channel, bandwidth, LTF segment, packet rate, the number of links and
   their mounting positions. Either:
   * reproduce a published setup on its own hardware (Intel 5300 or Atheros NICs with 3
     RX antennas; we have not checked their current availability), or
   * stay with ESP32 and add links. Community projects use 2–9 synchronised 1×1 links.
     Whether that gives useful pose accuracy has **not** been shown by any independent
     evaluation.
2. **Consent and ground truth.** Training labels need a camera-based pose "teacher"
   during data-collection sessions only. That requires **separate, explicit, recorded
   consent** from every participant for the camera, on top of the CSI recording consent.
   The camera must be visible and announced. It must **never be a hidden input and never
   be a runtime input**; RoomSense has no camera path, and none should be added to the
   runtime. Keep camera footage local. Extract keypoints and delete the footage when it
   is no longer needed or when a participant asks. Label spaces the user controls only.
3. **Synchronisation.** Timestamp CSI and video on one host clock. Fit each board's
   clock (`backend/roomsense/acquisition/alignment.py` does this), report the residual, and drop
   pairs whose alignment is uncertain.
4. **Dataset splits.** Split by **session, person and day**. The test set must contain
   people and days not seen in training, plus ideally a changed room layout. Leave a gap
   between train and test windows so no frames are shared. Never use random frame
   splits.
5. **Metrics.** Report PCK@0.2 (2D) and MPJPE / PA-MPJPE (3D), per joint and overall.
   Report the number of sessions, people and days. Give bootstrap confidence intervals
   over sessions. Always compare against a **mean-pose baseline** and a
   **constant-output baseline**, because a constant model can score well under a broken
   protocol (RuView's ADR-187 reports that its ADR-152 retracted such a figure). Evaluate
   through-wall conditions separately under the validation protocol.
6. **Compute (reported by the papers).** Training used 4 × Titan X (DensePose From
   WiFi); Person-in-WiFi 3D reports about 20 min per epoch in a comparison on one
   Nvidia 3090. Measure inference cost on the target machine.
7. **Packaging.** Export to safetensors or ONNX and put the file under
   `data/models/pose/`. Write a full manifest (`roomsense-pose-manifest-v1`) with the
   weights' SHA-256, the licence of the *weights*, the exact input requirements, and
   validation evidence. Set `measured_in_this_environment` only after local evaluation.
   A reviewed code change must also add an inference runtime to
   `AVAILABLE_INFERENCE_BACKENDS`; a manifest alone can never enable capability D. Even
   then, outputs are labelled EXPERIMENTAL and go to a research panel only
   (`backend/roomsense/inference/pose/interface.py`).

## 8. What could not be verified here

The egress proxy blocked arxiv.org (and its mirrors ar5iv and export.arxiv),
openaccess.thecvf.com, openreview.net, proceedings.neurips.cc, huggingface.co,
rfpose.csail.mit.edu, rfpose3d.csail.mit.edu, people.csail.mit.edu, dspace.mit.edu,
www.cis.upenn.edu, publications.ri.cmu.edu, www.ri.cmu.edu, semanticscholar.org and
0809zheng.github.io. The following are therefore **not verified**:

* whether later arXiv versions of DensePose From WiFi changed the hardware or released
  code;
* Person-in-WiFi's hardware details, sampling rate and split (from search summaries
  only);
* MM-Fi's Wi-Fi hardware, sampling rate and dataset licence (from a search summary
  only);
* RF-Pose / RF-Pose3D details and whether any code or data is available on request;
* the Hugging Face checkpoints and metrics cited by RuView;
* whether any of the community ESP32 results reproduce. None was re-run here, and no
  checkpoint was loaded.

Prices, current hardware availability and legal interpretation of the licences were not
researched and are not stated.

## 9. Sources inspected

| Source | Revision | Inspected |
|---|---|---|
| github.com/ruvnet/wifi-densepose | `5101504b72afc868a1be17bd6060651d21436251` (2025-06-09); HEAD `5ef001b4fef3e08433043ebd248e6121f1221528` (2026-09-27) | files and lines in §4.6; ADR-187; `cog-pose-estimation` README, `train_results.json`, `src/inference.rs` |
| github.com/davidakpele/wifi-densepose | `0d72e033ef4b5a9427641291d84113dd125069bf` | `src/hardware/csi_extractor.py`, `src/core/csi_processor.py`, `src/services/pose_service.py` |
| github.com/deletexiumu/wifi-densepose | `30071bfeaeef9dbfc47cf1e33336556b8ec2c448` | README audit; `v1/src/hardware/csi_extractor.py` |
| github.com/aiotgroup/Person-in-WiFi-3D-repo | `468aff35f0b042671b88bd22dc19428e5152f593` | README, LICENSE, `opera/datasets/wifi_pose.py`, `configs/wifi/petr_wifi.py`, `configs/_base_/datasets/wifi_keypoint.py` |
| github.com/aiotgroup/Person-in-WiFi-3D (project page) | `5fc6c5fe8e53e3316a022c47f9de844216f706a4` | `index.html`, `paper/2024CVPR_Person_in_WiFi_3D.pdf` |
| github.com/geekfeiw/wifiperson | `e0d091c5ba1784e61add18ffebbb1ca4ba3e60bc` | README, LICENSE, data-collection tools |
| github.com/ybhbingo/MMFi_dataset | `c9c1e3a8d5099779a618eae1bb351e253daa52d0` | README, `config.yaml`, `mmfi_lib/mmfi.py` |
| github.com/superstar1225/DensePose_from_WiFi | `1cedb239943e080810ebc5655c5d2ed22ad02d7c` | paper PDF copy, notebook |
| github.com/barleyjohn/DensePose-From-WiFi | `671196768b0b0da8e0d767d6205470b99af16774` | notebook |
| github.com/xyz38324/DensePose-from-WiFi | `33eafa45e6fa09f625b962f47b34541fcba40ae0` | README, file list |
| github.com/sel00000/csi-pose | `02b1001590beea8e7d6daac57a4d8b4a345eb50f` | README, LICENSE, weight-file search |
| github.com/gavmc/CSI | `b3130e0278964effa3d20bcc90a0a392a41b8cfa` | README, weight-file search |
| github.com/Abinand2631/Wifi-Vision | `5dacdc9683d9cd233ee1933480ad42d002b46583` | README, `train.py` header, weight-file search |
