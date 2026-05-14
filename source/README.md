# GPIO Module

Ada bindings to the ESP-IDF GPIO driver, structured as a two-level package hierarchy that separates the portable API from ESP32-S3-specific constants.

## Package overview

```
ESP32                       -- family root (esp32.ads)
└── ESP32.GPIO              -- portable GPIO API (esp32-gpio.ads/adb)

ESP32.S3                    -- S3 variant root (esp32-s3.ads)
├── ESP32.S3.GPIO           -- S3 pin constants and Safe_GPIO_Pin (esp32-s3-gpio.ads)
└── ESP32.S3.Interrupts     -- S3 interrupt source table (esp32-s3-interrupts.ads)
```

---

## `ESP32.GPIO` — portable API

`esp32-gpio.ads` / `esp32-gpio.adb`

Defines the types and thin Ada wrappers around the IDF `gpio_*` C functions that are common to all ESP32 variants.

### Types

| Type | Description |
|------|-------------|
| `GPIO_Pin` | Non-negative C integer; the widest range accepted by the IDF. Narrowed by variant-specific packages. |
| `GPIO_Mode` | `Mode_Disable`, `Mode_Input`, `Mode_Output`, `Mode_Output_Open_Drain`, `Mode_Input_Output_Open_Drain`, `Mode_Input_Output` |
| `GPIO_Intr_Type` | `Intr_Disabled`, `Intr_Positive_Edge`, `Intr_Negative_Edge`, `Intr_Any_Edge`, `Intr_Low_Level`, `Intr_High_Level` |
| `GPIO_Level` | `Low`, `High` |
| `GPIO_Pin_Config` | Aggregate record — see below. |

Both `GPIO_Mode` and `GPIO_Intr_Type` carry representation clauses so their values map directly onto the IDF enumeration integers.

### `GPIO_Pin_Config`

```ada
type GPIO_Pin_Config is record
   Reset_First      : Boolean       := True;
   Mode             : GPIO_Mode     := Mode_Disable;
   Pullup           : Boolean       := False;
   Pulldown         : Boolean       := False;
   Interrupt_Type   : GPIO_Intr_Type := Intr_Disabled;
   Interrupt_Enable : Boolean       := False;
end record
with Dynamic_Predicate => ...;
```

A `Dynamic_Predicate` enforces valid pull-resistor combinations at the point of use:

| Mode | Pullup | Pulldown |
|------|--------|----------|
| `Mode_Output` | must be `False` | must be `False` |
| `Mode_Output_Open_Drain`, `Mode_Input_Output_Open_Drain` | either | must be `False` |
| `Mode_Input`, `Mode_Input_Output` | either | either, but not both simultaneously |
| `Mode_Disable` | either | either, but not both simultaneously |

An aggregate that violates the predicate raises `Constraint_Error` before any IDF call is made.

### Procedures and functions

| Subprogram | IDF call |
|------------|----------|
| `Reset_Pin (Pin)` | `gpio_reset_pin` |
| `Set_Direction (Pin, Mode)` | `gpio_set_direction` |
| `Pullup_Enable (Pin)` | `gpio_pullup_en` |
| `Pullup_Disable (Pin)` | `gpio_pullup_dis` |
| `Pulldown_Enable (Pin)` | `gpio_pulldown_en` |
| `Pulldown_Disable (Pin)` | `gpio_pulldown_dis` |
| `Set_Intr_Type (Pin, Kind)` | `gpio_set_intr_type` |
| `Intr_Enable (Pin)` | `gpio_intr_enable` |
| `Intr_Disable (Pin)` | `gpio_intr_disable` |
| `Set_Level (Pin, Level)` | `gpio_set_level` |
| `Get_Level (Pin) return GPIO_Level` | `gpio_get_level` |
| `Configure_Pin (Pin, Config)` | applies all fields of `GPIO_Pin_Config` in order |

Every procedure raises `GPIO_Error` if the underlying IDF function returns a non-zero `esp_err_t`. The exception message includes the name of the failing IDF call and the raw error code.

`Configure_Pin` is the high-level entry point. It optionally resets the pin, sets direction, applies pull resistors, sets the interrupt trigger type, and enables or disables the interrupt — all in one call using the predicate-validated `GPIO_Pin_Config` record.

---

## `ESP32.S3.GPIO` — ESP32-S3 pin constants

`esp32-s3-gpio.ads`

Narrows the generic types to the concrete ESP32-S3 silicon.

```ada
GPIO_Min_Pin : constant := 0;
GPIO_Max_Pin : constant := 48;   -- 49 pins total

Flash_Pin_First : constant := 26;   -- internal SPI flash (SPI0/SPI1)
Flash_Pin_Last  : constant := 32;

PSRAM_Pin_First : constant := 33;   -- Octal PSRAM (e.g. ESP32-S3-WROOM-1-N8R8)
PSRAM_Pin_Last  : constant := 37;
```

### `Safe_GPIO_Pin`

```ada
subtype Safe_GPIO_Pin is
  ESP32.GPIO.GPIO_Pin range GPIO_Min_Pin .. GPIO_Max_Pin
with
  Static_Predicate =>
    Safe_GPIO_Pin not in Flash_Pin_First .. Flash_Pin_Last
    and Safe_GPIO_Pin not in PSRAM_Pin_First .. PSRAM_Pin_Last;
```

Using a reserved pin number as a static literal or named constant is a **compile-time error**. Dynamically produced values raise `Constraint_Error` at the point of assignment.

Modules without Octal PSRAM (e.g. a variant with only external flash) can declare their own subtype that excludes only the flash range — the two predicates are written independently for exactly this reason.

---

## `ESP32.S3.Interrupts` — interrupt source table

`esp32-s3-interrupts.ads`

Provides named `Interrupt_Source` constants for all 99 ESP32-S3 peripheral interrupt sources and the compile-time-safe subtype to use them with.

```ada
subtype Interrupt_Source is Ada.Interrupts.Interrupt_ID range 0 .. 98
with
  Static_Predicate =>
    Interrupt_Source /= 23
    and then Interrupt_Source /= 33
    and then Interrupt_Source /= 34
    and then Interrupt_Source /= 46;
```

Source IDs 23, 33, 34, and 46 are reserved by the hardware and have no peripheral assigned. Naming one of them in a static context is a compile-time error.

### Source groups

| Range | Group |
|-------|-------|
| 0–10 | Wireless / Bluetooth |
| 11–15 | Connectivity (I2C master, SLC, UHCI) |
| 16–19 | GPIO (`GPIO_Core_0 = 16`, `GPIO_Core_1 = 18`; NMI variants at 17, 19) |
| 20–22 | SPI (SPI1 = internal flash — do not use) |
| 24–26 | Audio / video (LCD_Cam, I2S0, I2S1) |
| 27–29 | UART (0–2) |
| 30–32 | Storage / PWM (SDIO_Host, PWM0, PWM1) |
| 35–45 | Miscellaneous (LEDC, EFuse, TWAI, USB_OTG, RTC_Core, RMT, PCNT, I2C_Ext0/1, SPI2/3_DMA) |
| 47–59 | Timers (WDT, TG0/1 timers and watchdogs, SysTimer targets) |
| 60–65 | Cache / memory management (SPI_Mem_Reject, D/ICache preload and sync, APB_ADC) |
| 66–75 | General-purpose DMA (in/out channels 0–4) |
| 76–78 | Crypto accelerators (RSA, AES, SHA) |
| 79–82 | Inter-processor interrupts (FreeRTOS at 79–80, IPC_ISR at 81–82) |
| 83–98 | Permission Management System (PMS) |

Pass any `Interrupt_Source` constant as the second argument to `pragma Attach_Handler` inside a protected type. GPIO interrupts use `GPIO_Core_0` (or `GPIO_Core_1` for handlers pinned to Core 1).

---

## Usage example

`gpio0_interrupt.ads` / `gpio0_interrupt.adb` show the complete pattern for a GPIO falling-edge interrupt using the Jorvik profile.

### Pin configuration

```ada
GPIO0 : constant ESP32.S3.GPIO.Safe_GPIO_Pin := 0;

Config : constant ESP32.GPIO.GPIO_Pin_Config :=
  (Reset_First      => True,
   Mode             => ESP32.GPIO.Mode_Input,
   Pullup           => True,
   Pulldown         => False,
   Interrupt_Type   => ESP32.GPIO.Intr_Negative_Edge,
   Interrupt_Enable => True);

ESP32.GPIO.Configure_Pin (GPIO0, Config);
```

`GPIO0` is typed as `Safe_GPIO_Pin`, so a reserved pin number is rejected at compile time.

### Interrupt handler

```ada
GPIO_Intr_Source : constant := ESP32.S3.Interrupts.GPIO_Core_0;

protected GPIO0_Handler is
   pragma Interrupt_Priority (System.Interrupt_Priority'Last);
   procedure On_Low;
   pragma Attach_Handler (On_Low, GPIO_Intr_Source);
   function Trigger_Count return Interfaces.Unsigned_32;
private
   Press_Count : Interfaces.Unsigned_32 := 0;
end GPIO0_Handler;
```

`Interrupt_Priority'Last` (255) maps to ESP-IDF C-callable level 3 — the highest priority at which an Ada handler can run. The full Ada priority → ESP-IDF level mapping is:

| Ada `Interrupt_Priority` | ESP-IDF flag | Xtensa level |
|--------------------------|-------------|--------------|
| 241–245 | `ESP_INTR_FLAG_LEVEL1` | 1 (lowest) |
| 246–250 | `ESP_INTR_FLAG_LEVEL2` | 2 |
| 251–255 | `ESP_INTR_FLAG_LEVEL3` | 3 (highest C-callable) |

`pragma Attach_Handler` with a static `Interrupt_Source` constant is the Jorvik-safe attachment form. Dynamic attachment via `Ada.Interrupts.Attach_Handler` raises `Program_Error` under `pragma Profile (Jorvik)`.

> **Hardware tip:** GPIO0 is the boot-mode strapping pin. Most development boards (e.g. ESP32-S3-DevKitC) wire a BOOT button between GPIO0 and GND, so no extra hardware is needed to exercise the interrupt counter example.
