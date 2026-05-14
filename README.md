# ESP32-S3 Ada Project Template (ESP-IDF Integrated)

[![Build](https://github.com/godunko/esp32s3_template/actions/workflows/main.yaml/badge.svg)](https://github.com/godunko/esp32s3_template/actions/workflows/main.yaml)

This repository provides a template for integrating Ada source code into the ESP-IDF (C-based) build system.
It allows you to leverage the robust drivers and RTOS capabilities of the ESP-IDF while writing your application logic in Ada.

The example application demonstrates GPIO input with a falling-edge interrupt handler,
written entirely in Ada using the Jorvik real-time profile.

## Project Architecture

Instead of a standalone Ada executable, this project compiles Ada source into a encapsulated static library that is linked into the final ESP-IDF project.

 * Ada Side: Managed by Alire (`alr`).
 * System Side: Managed by IDF (`CMake`/`ninja`).

## Prerequisites and Build Instructions

Prerequisites and build instructions are documented in the upstream repository this project was forked from:
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

## GPIO Abstraction Packages

The project introduces a two-level Ada package hierarchy for GPIO that separates the portable API from chip-specific details.

### `ESP32.GPIO` — portable API

`source/esp32-gpio.ads` / `esp32-gpio.adb` define the generic GPIO interface used by all ESP32 variants:

* `GPIO_Pin` — an unconstrained non-negative integer type accepted by the IDF.
* Direction, pull-up/pull-down, interrupt-type enumerations with IDF representation clauses.
* Thin Ada wrappers around the IDF C functions (`gpio_reset_pin`, `gpio_set_direction`, etc.).
* `GPIO_Error` exception raised on any IDF error return.

### `ESP32.S3.GPIO` — ESP32-S3 specifics

`source/esp32-s3-gpio.ads` narrows the portable types to the concrete ESP32-S3 silicon:

* `GPIO_Min_Pin = 0`, `GPIO_Max_Pin = 48` — the full S3 range.
* `Safe_GPIO_Pin` — a subtype with both a range constraint *and* a `Static_Predicate` that
  excludes the two reserved regions:
  * GPIO 26–32 — internal SPI flash (SPI0 / SPI1)
  * GPIO 33–37 — Octal PSRAM (on modules such as ESP32-S3-WROOM-1-N8R8)

  Assigning a reserved pin number to `Safe_GPIO_Pin` is caught at **compile time** when the
  value is a static literal or named constant.

### `ESP32.S3.Interrupts` — full interrupt source table

`source/esp32-s3-interrupts.ads` enumerates every one of the 99 ESP32-S3 peripheral
interrupt sources (0 .. 98) as named `Interrupt_Source` constants.

* `Interrupt_Source` — a subtype of `Ada.Interrupts.Interrupt_ID` constrained to
  `0 .. 98` with a `Static_Predicate` that excludes the four reserved slots that
  have no corresponding peripheral:

  | Reserved ID | Note |
  |-------------|------|
  | 23 | not assigned |
  | 33 | not assigned |
  | 34 | not assigned |
  | 46 | not assigned |

  Assigning a reserved value to `Interrupt_Source` is a **compile-time error** when
  the value is a static literal or named constant.  A runtime guard
  (`__gnat_is_valid_intr_source`, implemented in `freertos.c` using the IDF's own
  `esp_isr_names[]` table) catches any dynamically-produced reserved ID before it
  reaches `esp_intr_alloc`.

* `Max_Interrupt_Source : constant := 99` — the total count; deliberately left
  untyped so it can be used in contexts that require a universal integer.

* Named constants covering every source group — wireless/Bluetooth (0–15), GPIO
  (16–19), SPI (20–22), audio/video (24–26), UART (27–29), storage/PWM (30–32),
  miscellaneous peripherals (35–45), timers (47–59), cache/MMU (60–65), GDMA
  (66–75), crypto accelerators (76–78), inter-processor interrupts (79–82), and
  the Permission Management System (83–98).

The GPIO source IDs (`GPIO_Core_0 = 16`, `GPIO_Core_1 = 18`) previously lived in
`ESP32.S3.GPIO`; they have been moved here so that the GPIO package concerns itself
only with pin geometry and the interrupts package owns the interrupt matrix.

#### Target-portable source IDs

To avoid hard-coding numeric IDs that differ between ESP32 variants, the two values
that are used at run time — `ETS_GPIO_INTR_SOURCE` and `ETS_GPIO_INTR_SOURCE2` — are
exported from C via `soc/interrupts.h`:

```c
// freertos.c
int __gnat_gpio_intr_source_core0 = ETS_GPIO_INTR_SOURCE;   // 16 on S3
int __gnat_gpio_intr_source_core1 = ETS_GPIO_INTR_SOURCE2;  // 18 on S3, -1 on single-core
```

The Ada runtime imports these variables rather than using a literal, so the same
binary is correct for every ESP32 chip without any source changes.

## Interrupt Handling with the Jorvik Profile

The project uses `pragma Profile (Jorvik)`, which implies `No_Dynamic_Attachment`.
Dynamic calls such as `Ada.Interrupts.Attach_Handler` at run time raise `Program_Error`
under this profile.  Static attachment via `pragma Attach_Handler` inside a protected type
declaration is fully supported and is the idiomatic Jorvik approach.

A protected object with `pragma Interrupt_Priority` and `pragma Attach_Handler` maps
directly onto the ESP-IDF interrupt-matrix mechanism.  The runtime allocates a CPU interrupt
slot at elaboration time and registers the handler — no dynamic binding from user code.

```ada
protected GPIO0_Handler is
   --  Interrupt_Priority'First (25) selects ESP-IDF level 1 — the lowest
   --  C-callable hardware level.  See the priority mapping table below.
   pragma Interrupt_Priority (System.Interrupt_Priority'First);
   procedure On_Low;
   pragma Attach_Handler (On_Low, GPIO_Intr_Source);  --  static, Jorvik-safe
   function Trigger_Count return Interfaces.Unsigned_32;
private
   Press_Count : Interfaces.Unsigned_32 := 0;
end GPIO0_Handler;
```

`GPIO_Intr_Source` is a static constant (`ESP32.S3.Interrupts.GPIO_Core_0 = 16`), so
the attachment is resolved entirely at compile/elaboration time.

### Interrupt priority mapping

The Xtensa core has seven hardware interrupt levels.  Only levels 1–3 support standard
C calling conventions.  Levels 4–6 and NMI require hand-written assembly entry/exit
sequences and **cannot** be used with Ada protected handlers.

This runtime defines `System.Interrupt_Priority` as the range **25 .. 31** (7 values).
`System.ESPIDF.To_Flags` maps each value one-to-one onto an ESP-IDF flag:

| Ada `Interrupt_Priority` | ESP-IDF flag | Xtensa hardware level | Ada-safe? |
|--------------------------|--------------|----------------------|-----------|
| 25 (`'First`) | `ESP_INTR_FLAG_LEVEL1` | 1 (lowest) | ✓ |
| 26 | `ESP_INTR_FLAG_LEVEL2` | 2 | ✓ |
| 27 | `ESP_INTR_FLAG_LEVEL3` | 3 | ✓ |
| 28 | `ESP_INTR_FLAG_LEVEL4` | 4 — assembly only | ✗ |
| 29 | `ESP_INTR_FLAG_LEVEL5` | 5 — assembly only | ✗ |
| 30 | `ESP_INTR_FLAG_LEVEL6` | 6 — assembly only | ✗ |
| 31 (`'Last`) | `ESP_INTR_FLAG_NMI` | NMI — assembly only | ✗ |

Use `Interrupt_Priority'First` .. `Interrupt_Priority'First + 2` (values 25–27) for Ada
protected handlers.  Requesting level 4 or above will compile and elaborate but the
handler's C calling-convention entry/exit will be wrong at run time.

### How interrupt attachment reaches the ESP-IDF

When the Ada runtime elaborates a package that contains a protected object with
`pragma Attach_Handler`, it calls `System.Interrupts.Install_Restricted_Handlers`
(implemented in `crates/espidf_gnat_runtime/source/s-interr__espidf.adb`).  That routine
walks the handler array and calls `Install_Handler` once per source.  The full
chain from Ada to silicon is:

```
Ada protected object elaboration
  │
  └─► System.Interrupts.Install_Restricted_Handlers   (s-interr__espidf.adb)
        │  stores User_Handler  (Ada procedure pointer)
        │  stores PO_Priority   (Ada Interrupt_Priority value)
        └─► Install_Handler (Interrupt_ID)
              └─► System.ESPIDF.esp_intr_alloc
                    (source  => Interrupt_ID,
                     flags   => To_Flags (PO_Priority),   -- LEVEL1..NMI
                     handler => Default_Handler'Access,   -- C-convention trampoline
                     arg     => To_Address (Interrupt_ID),
                     handle  => null)
```

**`esp_intr_alloc`** programmes the Xtensa interrupt-matrix peripheral, connecting a
peripheral interrupt source to a CPU interrupt line and associating a C function pointer
and a single `void *` argument with that line.

**`Default_Handler`** is the C-convention procedure registered with `esp_intr_alloc`.
It receives the interrupt source ID as its argument, looks up the corresponding Ada
`Parameterless_Handler` in the `User_Handlers` table, and calls it directly:

```
CPU receives interrupt (hardware)
  │
  └─► Default_Handler(arg)              (s-interr__espidf.adb, Convention => C)
        │  arg → Interrupt_ID
        └─► User_Handlers(id).all       (Ada protected procedure, e.g. On_Low)
              └─► returns to FreeRTOS interrupt dispatcher
```

## Example: GPIO0 Falling-Edge Interrupt Counter

`source/gpio0_interrupt.ads` / `.adb` demonstrate the full pattern:

1. **Pin configuration** — `Initialize` calls the `ESP32.GPIO` API to configure GPIO0 as
   an input with pull-up enabled and a falling-edge interrupt:

   ```ada
   ESP32.GPIO.Reset_Pin        (GPIO0);
   ESP32.GPIO.Set_Direction    (GPIO0, ESP32.GPIO.Mode_Input);
   ESP32.GPIO.Pullup_Enable    (GPIO0);
   ESP32.GPIO.Pulldown_Disable (GPIO0);
   ESP32.GPIO.Set_Intr_Type    (GPIO0, ESP32.GPIO.Intr_Negative_Edge);
   ESP32.GPIO.Intr_Enable      (GPIO0);
   ```

   `GPIO0` is declared as `Safe_GPIO_Pin := 0`, so a typo that produces a reserved pin
   number would be rejected at compile time.

2. **Interrupt handler** — the protected procedure `On_Low` clears the GPIO interrupt
   status register then increments the counter:

   ```ada
   --  ESP32-S3 GPIO_STATUS_W1TC_REG (base 0x60004000, offset 0x4C).
   GPIO_Status_W1TC : Interfaces.Unsigned_32
     with Volatile, Import, Convention => Ada,
          Address => System.Storage_Elements.To_Address (16#6000_404C#);

   procedure On_Low is
   begin
      GPIO_Status_W1TC := Interfaces.Shift_Left (1, Natural (GPIO0));
      Press_Count := @ + 1;
   end On_Low;
   ```

   The register write is mandatory.  The GPIO peripheral aggregates interrupt status
   for all pins in a single register (`GPIO_STATUS_REG`).  Unlike most peripherals,
   the hardware does **not** clear that status automatically when the ISR runs — it
   stays asserted until software writes a 1 to the corresponding bit of
   `GPIO_STATUS_W1TC_REG` (the write-1-to-clear shadow).  If the status bit is not
   cleared before the handler returns, the CPU re-enters the ISR immediately and
   continuously, starving the FreeRTOS tick ISR and triggering the interrupt watchdog.

   Note that the status bit can be set before `Initialize` is ever called: the GPIO
   peripheral captures edges even while the CPU-level interrupt is masked.  Any edge
   that occurs during the reset and boot sequence (including strapping-pin sampling)
   will leave a pending status bit that fires the ISR the moment `esp_intr_alloc`
   enables the CPU interrupt during elaboration.

3. **Main loop** — `source/main.adb` polls `GPIO0_Interrupt.Trigger_Count` every 50 ms and
   prints a line each time the count changes:

   ```ada
   loop
      declare
         Count : constant Interfaces.Unsigned_32 := GPIO0_Interrupt.Trigger_Count;
      begin
         if Count /= Last_Count then
            Last_Count := Count;
            Ada.Text_IO.Put_Line ("GPIO0 interrupt count:" & Interfaces.Unsigned_32'Image (Count));
         end if;
      end;
      delay 0.05;
   end loop;
   ```

Pull GPIO0 to GND to trigger the interrupt and watch the counter increment on the serial
monitor.

### Compile-time safety

`Interrupt_Source` carries a `Static_Predicate` that rejects the four reserved IDs
(23, 33, 34, 46).  A static expression that names a reserved slot is a compile error,
not a silent misfire.

> **Tip:** GPIO0 is the ESP32-S3 boot-mode strapping pin.  Most development boards (e.g.
> ESP32-S3-DevKitC) already have a "BOOT" push button wired between GPIO0 and GND — so no
> extra hardware is needed to run this example.  Pressing BOOT while the firmware is running
> will fire the interrupt rather than resetting into the bootloader, because the chip only
> samples the strapping pin during reset.

`ESP-IDF` and `Ada & SPARK` extensions for VS Code creates useful development environment.

## VS Code Integration

To get the most out of this template, it is recommend installing the following extensions:

 * ESP-IDF Extension: Manages flashing, monitoring, and the SDK configuration (menuconfig).
 * Ada & SPARK Extension: Provides syntax highlighting, IntelliSense, and code navigation for Ada.

# Related repositories

 * [ESP-IDF GNAT Runtime](https://github.com/godunko/espidf_gnat_runtime)
 * [Ada/ESP-IDF Binding](https://github.com/godunko/espidf)
