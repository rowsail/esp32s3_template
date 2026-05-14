with Interfaces;
with System;
with ESP32.GPIO;
with ESP32.S3.GPIO;
with ESP32.S3.Interrupts;

package body GPIO0_Interrupt is
   use type Interfaces.Unsigned_32;

   GPIO0            : constant ESP32.S3.GPIO.Safe_GPIO_Pin := 0;
   GPIO_Intr_Source : constant := ESP32.S3.Interrupts.GPIO_Core_0;

   --  Thin Ada import of the C wrapper in freertos.c.  The wrapper calls
   --  gpio_ll_clear_intr_status / gpio_ll_clear_intr_status_high (both
   --  always_inline, so not directly linkable) with the global &GPIO device.
   --  GPIO_Clear_Intr_Status covers pins 0-31; _High covers pins 32+.
   procedure GPIO_Clear_Intr_Status (Mask : Interfaces.Unsigned_32)
     with Import, Convention => C,
          External_Name => "__gnat_gpio_clear_intr_status";

   protected GPIO0_Handler is
      --  Interrupt_Priority'First (25) maps to ESP_INTR_FLAG_LEVEL1.
      --  Levels 4+ require assembly entry/exit and cannot be used here.
      pragma Interrupt_Priority (System.Interrupt_Priority'First);

      procedure On_Low;
      pragma Attach_Handler (On_Low, GPIO_Intr_Source);

      function Trigger_Count return Interfaces.Unsigned_32;

   private
      Press_Count : Interfaces.Unsigned_32 := 0;
   end GPIO0_Handler;

   protected body GPIO0_Handler is

      procedure On_Low is
      begin
         GPIO_Clear_Intr_Status (Interfaces.Shift_Left (1, Natural (GPIO0)));
         Press_Count := @ + 1;
      end On_Low;

      function Trigger_Count return Interfaces.Unsigned_32 is
      begin
         return Press_Count;
      end Trigger_Count;

   end GPIO0_Handler;

   procedure Initialize is
      Config : constant ESP32.GPIO.GPIO_Pin_Config :=
        (Reset_First      => True,
         Mode             => ESP32.GPIO.Mode_Input,
         Pullup           => True,
         Pulldown         => False,
         Interrupt_Type   => ESP32.GPIO.Intr_Negative_Edge,
         Interrupt_Enable => True);
   begin
      ESP32.GPIO.Configure_Pin (GPIO0, Config);
   end Initialize;

   function Trigger_Count return Interfaces.Unsigned_32 is
   begin
      return GPIO0_Handler.Trigger_Count;
   end Trigger_Count;

end GPIO0_Interrupt;
