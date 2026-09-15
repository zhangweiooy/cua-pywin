# ONNX Runtime memory-option review

Date: 2026-09-15. Target release: 0.3.3.

## Selected production settings

```text
YOLO:      intra=4, inter=1, CPU arena=false, memory pattern=true
RapidOCR:  intra=4, inter=1, CPU arena=false
Florence:  intra=1, inter=1, CPU arena=false, memory pattern=true
```

Memory-pattern reuse remains enabled because disabling it did not improve the
selected Florence configuration and increased stable latency when combined
with a disabled arena.

## Process-isolated component results

Each component used the same Oryx screenshot or icon fixture. Ten repeated
inferences followed model loading and the first inference.

| Component | Change | Stable P50 | Stable RSS | Private Bytes | Threads |
| --- | --- | ---: | ---: | ---: | ---: |
| YOLO | baseline | 559 ms | 936 MiB | 1,942 MiB | 52 |
| YOLO | CPU arena off | 607 ms | 175 MiB | 885 MiB | 52 |
| RapidOCR | automatic threads | 2,112 ms | 438 MiB | 1,145 MiB | 103 |
| RapidOCR | 4/1 threads | 1,174 ms | 429 MiB | 1,136 MiB | 64 |
| Florence | baseline | 639 ms | 865 MiB | 1,665 MiB | 30 |
| Florence | CPU arena off | 661 ms | 604 MiB | 1,317 MiB | 30 |

All compared component outputs were identical. RapidOCR with four threads also
completed 100 repeated calls without an exception; P50 was 944 ms and P95 was
992 ms for that run.

## Full pipeline results

Thirty repeated full pipeline calls used the same screenshot and produced the
same eight icons, 46 OCR strings, and eight Florence captions.

| Metric | Baseline | Selected settings |
| --- | ---: | ---: |
| Load | 2,291 ms | 1,612 ms |
| First inference | 5,303 ms | 5,411 ms |
| Stable P50 | 2,926 ms | 2,706 ms |
| Stable P95 | 4,776 ms | 4,473 ms |
| Stable RSS | 2,052 MiB | 1,165 MiB |
| Private Bytes | 3,101 MiB | 1,872 MiB |
| Peak RSS | 2,394 MiB | 1,545 MiB |
| Peak Private Bytes | 3,443 MiB | 2,252 MiB |

## Packaged-runtime verification

Separate 0.3.2-baseline and optimized-candidate standalone directories processed
the same WGC-captured Oryx fixture through YOLO, RapidOCR, and Florence. The
candidate contained the 0.3.3 runtime changes but was measured before its
package-version metadata was bumped. Both returned six icons, 45 text nodes,
and six captions.

| Metric | 0.3.2 baseline | Optimized candidate |
| --- | ---: | ---: |
| Full call | 5,784 ms | 3,722 ms |
| Idle Working Set after call | 1,317 MiB | 879 MiB |
| Idle Private Bytes after call | 2,177 MiB | 1,554 MiB |
| Peak Working Set | 1,406 MiB | 1,190 MiB |
| Threads after call | 102 | 65 |

The candidate passed the complete 482-test suite, release validation, Teaching
lifecycle smoke, and packaged SOM/OCR/Florence smoke. These measurements are
machine-specific and do not replace the broader desktop accuracy matrix.
