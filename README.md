# ESP32-S3 Ada Project Template — Interrupt Support Fork

[![Build](https://github.com/godunko/esp32s3_template/actions/workflows/main.yaml/badge.svg)](https://github.com/godunko/esp32s3_template/actions/workflows/main.yaml)

This repository is a fork of
[godunko/esp32s3_template](https://github.com/godunko/esp32s3_template).
It extends the upstream template with a complete interrupt-handling framework:
all 99 ESP32-S3 peripheral interrupt sources are modelled in Ada, the Ada
ceiling-priority is wired through to the ESP-IDF hardware-level allocator, and
compile-time and runtime guards prevent misuse of reserved interrupt source IDs.

For project architecture, prerequisites, and build instructions refer to the
upstream README at
[godunko/esp32s3_template](https://github.com/godunko/esp32s3_template).

Once built and flashed you should see:

```
Hello, Ada world!

GPIO0 is configured as INPUT_PULLUP with falling-edge interrupt
Pull GPIO0 low to trigger the Ada interrupt procedure

GPIO0 interrupt count: 1
GPIO0 interrupt count: 2
...
```

> **Tip:** GPIO0 is the ESP32-S3 boot-mode strapping pin.  Most development
> boards (e.g. ESP32-S3-DevKitC) have a "BOOT" button wired between GPIO0 and
> GND — pressing it while the firmware is running fires the interrupt rather
> than resetting into the bootloader, because the chip only samples the
> strapping pin at power-on.

---

## Changes from upstream

### `ESP32.S3.Interrupts` — full interrupt source table

The upstream template handled only the two GPIO interrupt sources and expressed
them as bare integer constants in `ESP32.S3.GPIO`.  This fork introduces
`source/esp32-s3-interrupts.ads`, which:

* Moves the GPIO source IDs out of `ESP32.S3.GPIO` so that the GPIO package
  concerns itself only with pin geometry.
* Adds a named constant for **every one of the 99 ESP32-S3 peripheral interrupt
  sources** (0 .. 98), grouped by peripheral family:

  | Range | Group |
  |-------|-------|
  | 0–15 | Wireless / Bluetooth |
  | 16–19 | GPIO (Core 0 and Core 1, normal and NMI variants) |
  | 20–22 | SPI (note: SPI1 is the internal flash bus — do not use) |
  | 24–26 | LCD / I²S audio/video |
  | 27–29 | UART |
  | 30–32 | SDIO / PWM |
  | 35–45 | Miscellaneous (LEDC, eFuse, TWAI/CAN, USB OTG, RTC, RMT, PCNT, I²C, DMA) |
  | 47–59 | Timers (WDT, TGn, system timer) |
  | 60–65 | Cache / MMU |
  | 66–75 | General-purpose DMA (in/out channels 0–4) |
  | 76–78 | Crypto accelerators (RSA, AES, SHA) |
  | 79–82 | Inter-processor interrupts (FreeRTOS / IPC) |
  | 83–98 | Permission Management System (PMS) |

* Defines a **`Static_Predicate`** on the `Interrupt_Source` subtype that
  excludes the four reserved slots that have no peripheral:

  | Reserved ID | Reason |
  |-------------|--------|
  | 23 | not assigned on ESP32-S3 |
  | 33 | not assigned on ESP32-S3 |
  | 34 | not assigned on ESP32-S3 |
  | 46 | not assigned on ESP32-S3 |

  Using a reserved ID in a static context (literal, named constant) is a
  **compile-time error**.

#### Target-portable source IDs

The numeric values of `ETS_GPIO_INTR_SOURCE` and `ETS_GPIO_INTR_SOURCE2`
differ between ESP32 variants.  To avoid embedding chip-specific numbers in
Ada source, the two values used at run time are exported from C via
`soc/interrupts.h`:

```c
// crates/espidf_gnat_runtime/source/freertos.c
const int __gnat_gpio_intr_source_core0 = ETS_GPIO_INTR_SOURCE;
#ifdef ETS_GPIO_INTR_SOURCE2
const int __gnat_gpio_intr_source_core1 = ETS_GPIO_INTR_SOURCE2;
#else
const int __gnat_gpio_intr_source_core1 = -1;  // single-core chip: not present
#endif
```

The Ada runtime (`s-interr.adb`) imports these as `Interfaces.C.int` variables
and uses them at elaboration time.  No Ada source file needs editing when
retargeting to a different ESP32 variant.

---

### Interrupt priority wired through to ESP-IDF

The upstream runtime allocated every interrupt handler at ESP-IDF level 1
regardless of the Ada ceiling priority declared in the protected object.  This
fork threads the `Interrupt_Priority` value all the way through:

```
Install_Restricted_Handlers (Prio, Handlers)   -- s-interr.adb
  └─► Install_Handler (Interrupt, Prio)
        └─► __gnat_esp_intr_alloc_c_handler (source, ada_interrupt_priority, …)
              └─► esp_intr_alloc (source, ESP_INTR_FLAG_LEVELn, …)
```

`__gnat_esp_intr_alloc_c_handler` (in `freertos.c`) maps the Ada
`Interrupt_Priority` range proportionally onto the three ESP-IDF C-callable
hardware levels:

| Ada `Interrupt_Priority` | ESP-IDF flag | Xtensa hardware level |
|--------------------------|--------------|----------------------|
| 241–245 | `ESP_INTR_FLAG_LEVEL1` | 1 (lowest) |
| 246–250 | `ESP_INTR_FLAG_LEVEL2` | 2 |
| 251–255 | `ESP_INTR_FLAG_LEVEL3` | 3 (highest C-callable) |

The Xtensa core has seven hardware interrupt levels.  Levels 4, 5, and NMI
require hand-written assembly entry points and cannot invoke C (or Ada)
functions.  The mapping deliberately caps at level 3.
`Interrupt_Priority'Last` (255) therefore requests the highest level at which
an Ada handler can safely execute.

---

### Full call path from Ada to silicon

#### Elaboration (handler registration)

When the runtime elaborates a package containing a protected object with
`pragma Attach_Handler`, the following chain executes:

```
Ada protected object elaboration
  │
  └─► System.Interrupts.Install_Restricted_Handlers   (s-interr.adb)
        │  records: User_Handlers[source] ← Ada procedure pointer
        │           Source_Args[source]   ← source ID (C int)
        └─► Install_Handler (source id, Ada ceiling priority)
              │
              ├─► __gnat_is_valid_intr_source (freertos.c)
              │     checks esp_isr_names[source] != NULL
              │     raises Program_Error for reserved or out-of-range IDs
              │
              └─► __gnat_esp_intr_alloc_c_handler (freertos.c)
                    maps Ada priority 241..255 → ESP_INTR_FLAG_LEVELn
                    └─► esp_intr_alloc (source, flags,
                              Interrupt_Trampoline, &Source_Args[source],
                              &handle)
```

`esp_intr_alloc` programmes the Xtensa interrupt-matrix peripheral, connecting
the peripheral source to a CPU interrupt line at the requested hardware level
and binding `Interrupt_Trampoline` as the handler.

#### Dispatch (interrupt fires)

```
CPU receives interrupt (hardware)
  │
  └─► Interrupt_Trampoline(arg)             (s-interr.adb, Convention => C)
        │  arg → &Source_Args[n] → source ID
        │
        ├─► if source == GPIO Core-0 or Core-1:
        │     gpio_ll_get_intr_status        (hal/gpio_ll.h)
        │     gpio_ll_clear_intr_status      (hal/gpio_ll.h)
        │     (GPIO is the only source whose status register the runtime
        │      must clear; all other peripherals do this in their own handler)
        │
        └─► User_Handlers[source_id].all     (Ada protected procedure)
              └─► returns to FreeRTOS interrupt dispatcher
```

#### Source-ID validation

`__gnat_is_valid_intr_source` checks the ESP-IDF internal table
`esp_isr_names[]`.  Each entry is either a non-NULL name string (valid source)
or `NULL` (reserved / not present on this chip).  This makes the check
chip-portable with no Ada-side chip knowledge.

---

### Compile-time and runtime safety summary

| Layer | Mechanism | What it catches |
|-------|-----------|-----------------|
| Compile time | `Static_Predicate` on `Interrupt_Source` | Reserved IDs (23, 33, 34, 46) used as static expressions |
| Run time | `__gnat_is_valid_intr_source` / `esp_isr_names[]` | Reserved IDs produced dynamically; IDs outside 0..98 |
| Run time | `esp_intr_alloc` return-value check | IDF allocation failures (e.g. no free CPU interrupt line) |

---

## VS Code Integration

The following extensions are recommended:

* **ESP-IDF Extension** — build, flash, monitor, menuconfig.
* **Ada & SPARK Extension** — syntax highlighting, IntelliSense, code navigation.

## Related repositories

* [godunko/esp32s3_template](https://github.com/godunko/esp32s3_template) — upstream template this fork is based on
* [rowsail/espidf_gnat_runtime](https://github.com/rowsail/espidf_gnat_runtime) — forked runtime with interrupt support changes
* [godunko/espidf_gnat_runtime](https://github.com/godunko/espidf_gnat_runtime) — upstream runtime
* [godunko/espidf](https://github.com/godunko/espidf) — Ada/ESP-IDF binding
