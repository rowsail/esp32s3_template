with Interfaces;
with System;
with System.Storage_Elements;
with ESP32.GPIO;
with ESP32.S3.GPIO;
with ESP32.S3.Interrupts;

package body GPIO0_Interrupt is
   use type Interfaces.Unsigned_32;

   GPIO0            : constant ESP32.S3.GPIO.Safe_GPIO_Pin := 0;
   GPIO_Intr_Source : constant := ESP32.S3.Interrupts.GPIO_Core_0;

   --  ESP32-S3 GPIO_STATUS_W1TC_REG: write 1 to clear a GPIO interrupt status
   --  bit.  Base 0x60004000 + offset 0x4C.  Must be cleared in every ISR
   --  invocation or the hardware re-asserts the interrupt immediately.
   GPIO_Status_W1TC : Interfaces.Unsigned_32
     with Volatile,
          Import,
          Convention => Ada,
          Address    => System.Storage_Elements.To_Address (16#6000_404C#);

   protected GPIO0_Handler is
      --  Ada protected-object ceiling priority.  The runtime maps the full
      --  Interrupt_Priority range (241 .. 255) onto ESP-IDF C-callable
      --  levels 1 .. 3; Last therefore selects level 3 (medium priority).
      --  High-level assembly-entry levels (4/5/NMI) are never used.
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
         --  Clear the interrupt status bit to prevent immediate re-entry. 
         --  This is a write-1-to-clear register: writing 0 has no effect. 
         --  This would be better done in a more Ada like way, so this is currently a hack.
         GPIO_Status_W1TC := Interfaces.Shift_Left (1, Natural (GPIO0));
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
