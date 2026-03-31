# Cornerstone
Code written for the PM class' Cornerstone team WALL-E. All code done here is primarily completed and maintained by Caitlyn.

## Code Notes:
This is just to track work flow for future reference if at all needed.

### Process:
Since I'm most comfortable and familiar I will write a demo framework in MicroPython. Due to my familiarity with it, it will allow for quick production flow and debugging and understanding the task at hand. From there I will translate it into C or a C derivative for optimization purposes. C is more optimized and generally performs faster with less pull on memory. **In short: MicroPython ==> C or C++**

### Code Outline:
The code will be written in the following stages:

1. Set up Pico (I2C) and INA219 (sensor) communications
2. Collect initial data (use time) and sleep (ex only collect a sample every 10 minutes)
3. Build an initial schedule
4. Perform periodic checks and adjust the schedule if needed
5. Test with mock data (mock work flow)