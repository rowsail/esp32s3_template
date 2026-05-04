--  ESP32-S3 interrupt matrix source IDs.
--  These constants map to the periph_interrupt_t enum defined in
--  esp-idf/components/soc/esp32s3/include/soc/interrupts.h.
--
--  Each constant names a peripheral that can be routed through the interrupt
--  matrix to one of the 32 CPU interrupt lines.  Pass the constant as the
--  Interrupt_ID argument to Ada.Interrupts.Attach_Handler or
--  pragma Attach_Handler.
--
--  Source values 23, 33, 34, and 46 are reserved by hardware and have no
--  named constant here.  ETS_SYSTIMER_TARGET*_EDGE names are aliases for the
--  corresponding TARGET*_INTR names and are omitted to avoid confusion.
with Ada.Interrupts;

package ESP32.S3.Interrupts is

   use type Ada.Interrupts.Interrupt_ID;

    --  Total number of interrupt sources on the ESP32-S3.
    Max_Interrupt_Source : constant Ada.Interrupts.Interrupt_ID := 99;

    --  Compile-time-safe interrupt source subtype for ESP32-S3.
    --  Reserved source IDs are excluded by the static predicate.
    subtype Interrupt_Source is Ada.Interrupts.Interrupt_ID range 0 .. 98
    with
       Static_Predicate =>
          Interrupt_Source /= 23
          and then Interrupt_Source /= 33
          and then Interrupt_Source /= 34
          and then Interrupt_Source /= 46;

   --  -----------------------------------------------------------------------
   --  Wireless / Bluetooth  (sources 0 .. 15)
   --  -----------------------------------------------------------------------
   Wifi_Mac           : constant Interrupt_Source := 0;
   Wifi_Mac_NMI       : constant Interrupt_Source := 1;
   Wifi_Pwr           : constant Interrupt_Source := 2;
   Wifi_BB            : constant Interrupt_Source := 3;
   BT_Mac             : constant Interrupt_Source := 4;
   BT_BB              : constant Interrupt_Source := 5;
   BT_BB_NMI          : constant Interrupt_Source := 6;
   RWBT               : constant Interrupt_Source := 7;
   RWBLE              : constant Interrupt_Source := 8;
   RWBT_NMI           : constant Interrupt_Source := 9;
   RWBLE_NMI          : constant Interrupt_Source := 10;

   --  -----------------------------------------------------------------------
   --  Connectivity peripherals  (sources 11 .. 15)
   --  -----------------------------------------------------------------------
   I2C_Master         : constant Interrupt_Source := 11;
   SLC0               : constant Interrupt_Source := 12;
   SLC1               : constant Interrupt_Source := 13;
   UHCI0              : constant Interrupt_Source := 14;
   UHCI1              : constant Interrupt_Source := 15;

   --  -----------------------------------------------------------------------
   --  GPIO  (sources 16 .. 19)
   --  Source 16 is used on Core 0; source 18 is used on Core 1.
   --  NMI variants (17, 19) bypass the normal interrupt mask mechanism.
   --  -----------------------------------------------------------------------
   GPIO_Core_0        : constant Interrupt_Source := 16;
   GPIO_Core_0_NMI    : constant Interrupt_Source := 17;
   GPIO_Core_1        : constant Interrupt_Source := 18;
   GPIO_Core_1_NMI    : constant Interrupt_Source := 19;

   --  -----------------------------------------------------------------------
   --  SPI  (sources 20 .. 22)
   --  SPI1 is the internal flash bus -- do not use.
   --  -----------------------------------------------------------------------
   SPI1               : constant Interrupt_Source := 20;  --  internal flash, do not use
   SPI2               : constant Interrupt_Source := 21;
   SPI3               : constant Interrupt_Source := 22;
   --  23 is reserved

   --  -----------------------------------------------------------------------
   --  Audio / video  (sources 24 .. 26)
   --  -----------------------------------------------------------------------
   LCD_Cam            : constant Interrupt_Source := 24;
   I2S0               : constant Interrupt_Source := 25;
   I2S1               : constant Interrupt_Source := 26;

   --  -----------------------------------------------------------------------
   --  UART  (sources 27 .. 29)
   --  -----------------------------------------------------------------------
   UART0              : constant Interrupt_Source := 27;
   UART1              : constant Interrupt_Source := 28;
   UART2              : constant Interrupt_Source := 29;

   --  -----------------------------------------------------------------------
   --  Storage / PWM  (sources 30 .. 32)
   --  -----------------------------------------------------------------------
   SDIO_Host          : constant Interrupt_Source := 30;
   PWM0               : constant Interrupt_Source := 31;
   PWM1               : constant Interrupt_Source := 32;
   --  33, 34 are reserved

   --  -----------------------------------------------------------------------
   --  Miscellaneous peripherals  (sources 35 .. 45)
   --  -----------------------------------------------------------------------
   LEDC               : constant Interrupt_Source := 35;
   EFuse              : constant Interrupt_Source := 36;
   TWAI               : constant Interrupt_Source := 37;  --  CAN / TWAI controller
   USB_OTG            : constant Interrupt_Source := 38;
   RTC_Core           : constant Interrupt_Source := 39;  --  includes RTC watchdog
   RMT                : constant Interrupt_Source := 40;
   PCNT               : constant Interrupt_Source := 41;
   I2C_Ext0           : constant Interrupt_Source := 42;
   I2C_Ext1           : constant Interrupt_Source := 43;
   SPI2_DMA           : constant Interrupt_Source := 44;
   SPI3_DMA           : constant Interrupt_Source := 45;
   --  46 is reserved

   --  -----------------------------------------------------------------------
   --  Timers  (sources 47 .. 59)
   --  -----------------------------------------------------------------------
   WDT                : constant Interrupt_Source := 47;
   Timer1             : constant Interrupt_Source := 48;
   Timer2             : constant Interrupt_Source := 49;
   TG0_T0             : constant Interrupt_Source := 50;
   TG0_T1             : constant Interrupt_Source := 51;
   TG0_WDT            : constant Interrupt_Source := 52;
   TG1_T0             : constant Interrupt_Source := 53;
   TG1_T1             : constant Interrupt_Source := 54;
   TG1_WDT            : constant Interrupt_Source := 55;
   Cache_IA           : constant Interrupt_Source := 56;
   SysTimer_Target0   : constant Interrupt_Source := 57;
   SysTimer_Target1   : constant Interrupt_Source := 58;
   SysTimer_Target2   : constant Interrupt_Source := 59;

   --  -----------------------------------------------------------------------
   --  Cache / memory management  (sources 60 .. 65)
   --  -----------------------------------------------------------------------
   SPI_Mem_Reject     : constant Interrupt_Source := 60;
   DCache_Preload0    : constant Interrupt_Source := 61;
   ICache_Preload0    : constant Interrupt_Source := 62;
   DCache_Sync0       : constant Interrupt_Source := 63;
   ICache_Sync0       : constant Interrupt_Source := 64;
   APB_ADC            : constant Interrupt_Source := 65;

   --  -----------------------------------------------------------------------
   --  General-purpose DMA  (sources 66 .. 75)
   --  -----------------------------------------------------------------------
   DMA_In_Ch0         : constant Interrupt_Source := 66;
   DMA_In_Ch1         : constant Interrupt_Source := 67;
   DMA_In_Ch2         : constant Interrupt_Source := 68;
   DMA_In_Ch3         : constant Interrupt_Source := 69;
   DMA_In_Ch4         : constant Interrupt_Source := 70;
   DMA_Out_Ch0        : constant Interrupt_Source := 71;
   DMA_Out_Ch1        : constant Interrupt_Source := 72;
   DMA_Out_Ch2        : constant Interrupt_Source := 73;
   DMA_Out_Ch3        : constant Interrupt_Source := 74;
   DMA_Out_Ch4        : constant Interrupt_Source := 75;

   --  -----------------------------------------------------------------------
   --  Crypto accelerators  (sources 76 .. 78)
   --  -----------------------------------------------------------------------
   RSA                : constant Interrupt_Source := 76;
   AES                : constant Interrupt_Source := 77;
   SHA                : constant Interrupt_Source := 78;

   --  -----------------------------------------------------------------------
   --  Inter-processor interrupts  (sources 79 .. 82)
   --  79-80 are used internally by FreeRTOS; 81-82 by IPC_ISR.
   --  -----------------------------------------------------------------------
   From_CPU_Intr0     : constant Interrupt_Source := 79;  --  FreeRTOS
   From_CPU_Intr1     : constant Interrupt_Source := 80;  --  FreeRTOS
   From_CPU_Intr2     : constant Interrupt_Source := 81;  --  IPC_ISR
   From_CPU_Intr3     : constant Interrupt_Source := 82;  --  IPC_ISR

   --  -----------------------------------------------------------------------
   --  Security / PMS (Permission Management System)  (sources 83 .. 98)
   --  -----------------------------------------------------------------------
   Assist_Debug                : constant Interrupt_Source := 83;
   DMA_APBPeri_PMS             : constant Interrupt_Source := 84;
   Core0_IRAM0_PMS             : constant Interrupt_Source := 85;
   Core0_DRAM0_PMS             : constant Interrupt_Source := 86;
   Core0_PIF_PMS               : constant Interrupt_Source := 87;
   Core0_PIF_PMS_Size          : constant Interrupt_Source := 88;
   Core1_IRAM0_PMS             : constant Interrupt_Source := 89;
   Core1_DRAM0_PMS             : constant Interrupt_Source := 90;
   Core1_PIF_PMS               : constant Interrupt_Source := 91;
   Core1_PIF_PMS_Size          : constant Interrupt_Source := 92;
   Backup_PMS_Violate          : constant Interrupt_Source := 93;
   Cache_Core0_ACS             : constant Interrupt_Source := 94;
   Cache_Core1_ACS             : constant Interrupt_Source := 95;
   USB_Serial_JTAG             : constant Interrupt_Source := 96;
   Peri_Backup                 : constant Interrupt_Source := 97;
   DMA_ExtMem_Reject           : constant Interrupt_Source := 98;

end ESP32.S3.Interrupts;
