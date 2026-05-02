#Run ctrl+shift+p and then select "MicroPico: Run..." to test if the Pico is connected. Light will turn on and hello world will be printed
from machine import Pin

led = Pin(0, Pin.OUT)
led.value(1)   # turn LED ON
led.value(0)   # turn LED OFF

print("Hello, World!")