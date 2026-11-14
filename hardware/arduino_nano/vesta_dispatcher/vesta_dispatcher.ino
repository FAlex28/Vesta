#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <string.h>

// Vesta physical dispatcher for an Arduino Nano (ATmega328P).
// The PC owns Wi-Fi and sends newline-terminated commands over USB Serial.

LiquidCrystal_I2C lcd(0x27, 16, 2);

const uint8_t LED_POWER = 2;
const uint8_t LED_LINK = 3;
const uint8_t LED_DELIVERY_ERROR = 4;
const uint8_t LED_ALERT = 5;
const uint8_t LED_RESOLVED = 6;
const uint8_t LEDS[] = {
  LED_POWER,
  LED_LINK,
  LED_DELIVERY_ERROR,
  LED_ALERT,
  LED_RESOLVED,
};

const uint8_t BUZZER_ALERT = 7;
const uint8_t BUZZER_FEEDBACK = 8;

const uint8_t PIN_LIGHT = A0;
const uint8_t PIN_HUMIDITY = A1;
const uint8_t PIN_JOYSTICK_X = A2;
const uint8_t PIN_JOYSTICK_Y = A3;
const uint8_t PIN_JOYSTICK_SWITCH = 9;

const unsigned long PC_TIMEOUT_MS = 10000UL;
const unsigned long SENSOR_INTERVAL_MS = 250UL;
const unsigned long LCD_INTERVAL_MS = 150UL;
const unsigned long BUTTON_DEBOUNCE_MS = 35UL;
const unsigned long RESOLVED_DISPLAY_MS = 5000UL;
const unsigned long BUTTON_DISPLAY_MS = 1000UL;
const unsigned long DELIVERED_LED_MS = 1200UL;

const uint8_t SERIAL_BUFFER_SIZE = 120;
char serialBuffer[SERIAL_BUFFER_SIZE];
uint8_t serialLength = 0;
bool serialOverflow = false;

bool pcLinked = false;
bool alertActive = false;
bool deliveryError = false;
unsigned long lastPcMessageAt = 0;
unsigned long resolvedDisplayUntil = 0;
unsigned long buttonDisplayUntil = 0;
unsigned long deliveredLedUntil = 0;

char activeAlertId[40] = "";
char alertReason[24] = "";

int lightValue = 0;
int humidityValue = 0;
int joystickXValue = 0;
int joystickYValue = 0;

int lastButtonReading = HIGH;
int stableButtonState = HIGH;
unsigned long buttonChangedAt = 0;

unsigned long lastSensorReadAt = 0;
unsigned long lastLcdUpdateAt = 0;
char lastLcdLine1[17] = "";
char lastLcdLine2[17] = "";

const uint8_t SOUND_NONE = 0;
const uint8_t SOUND_RESOLVED = 1;
const uint8_t SOUND_ERROR = 2;
const uint8_t SOUND_BUTTON = 3;

uint8_t oneShotSound = SOUND_NONE;
unsigned long oneShotStartedAt = 0;
int activeTonePin = -1;
unsigned int activeToneFrequency = 0;

void copyText(char *destination, size_t destinationSize, const char *source) {
  if (destinationSize == 0) {
    return;
  }
  if (source == NULL) {
    destination[0] = '\0';
    return;
  }
  strncpy(destination, source, destinationSize - 1);
  destination[destinationSize - 1] = '\0';
}

bool deadlineActive(unsigned long now, unsigned long deadline) {
  return deadline != 0 && (long)(deadline - now) > 0;
}

void setToneOutput(int pin, unsigned int frequency) {
  if (pin == activeTonePin && frequency == activeToneFrequency) {
    return;
  }

  if (activeTonePin >= 0) {
    noTone((uint8_t)activeTonePin);
  }

  activeTonePin = pin;
  activeToneFrequency = frequency;
  if (pin >= 0 && frequency > 0) {
    tone((uint8_t)pin, frequency);
  }
}

void stopToneOutput() {
  setToneOutput(-1, 0);
}

void startOneShot(uint8_t sound, unsigned long now) {
  oneShotSound = sound;
  oneShotStartedAt = now;
}

void updateOneShotSound(unsigned long now) {
  const unsigned long elapsed = now - oneShotStartedAt;

  if (oneShotSound == SOUND_RESOLVED) {
    if (elapsed < 120UL) {
      setToneOutput(BUZZER_FEEDBACK, 1500);
    } else if (elapsed < 220UL) {
      stopToneOutput();
    } else if (elapsed < 360UL) {
      setToneOutput(BUZZER_FEEDBACK, 1900);
    } else {
      oneShotSound = SOUND_NONE;
      stopToneOutput();
    }
    return;
  }

  if (oneShotSound == SOUND_ERROR) {
    if (elapsed < 220UL) {
      setToneOutput(BUZZER_FEEDBACK, 500);
    } else {
      oneShotSound = SOUND_NONE;
      stopToneOutput();
    }
    return;
  }

  if (oneShotSound == SOUND_BUTTON) {
    if (elapsed < 90UL) {
      setToneOutput(BUZZER_FEEDBACK, 1000);
    } else {
      oneShotSound = SOUND_NONE;
      stopToneOutput();
    }
  }
}

void updateAlertSound(unsigned long now) {
  const unsigned long phase = now % 1600UL;

  if (phase < 250UL) {
    setToneOutput(BUZZER_ALERT, 1200);
  } else if (phase < 400UL) {
    stopToneOutput();
  } else if (phase < 650UL) {
    setToneOutput(BUZZER_ALERT, 1200);
  } else if (phase < 800UL) {
    stopToneOutput();
  } else if (phase < 1050UL) {
    setToneOutput(BUZZER_ALERT, 1600);
  } else {
    stopToneOutput();
  }
}

void updateSound(unsigned long now) {
  if (oneShotSound != SOUND_NONE) {
    updateOneShotSound(now);
  } else if (alertActive) {
    updateAlertSound(now);
  } else {
    stopToneOutput();
  }
}

void markPcActivity(unsigned long now) {
  pcLinked = true;
  lastPcMessageAt = now;
}

void printStatus() {
  Serial.print(F("STATUS|PC="));
  Serial.print(pcLinked ? 1 : 0);
  Serial.print(F("|ALERT="));
  Serial.print(alertActive ? 1 : 0);
  Serial.print(F("|ERROR="));
  Serial.print(deliveryError ? 1 : 0);
  Serial.print(F("|L="));
  Serial.print(lightValue);
  Serial.print(F("|U="));
  Serial.print(humidityValue);
  Serial.print(F("|JX="));
  Serial.print(joystickXValue);
  Serial.print(F("|JY="));
  Serial.print(joystickYValue);
  Serial.print(F("|JOY="));
  Serial.print(stableButtonState == LOW ? 1 : 0);
  Serial.print(F("|ID="));
  Serial.println(activeAlertId);
}

void acknowledge(const __FlashStringHelper *command) {
  Serial.print(F("OK|"));
  Serial.println(command);
}

void processCommand(char *line, unsigned long now) {
  char *savePointer = NULL;
  char *command = strtok_r(line, "|", &savePointer);
  if (command == NULL || command[0] == '\0') {
    return;
  }

  markPcActivity(now);

  if (strcmp(command, "HELLO") == 0) {
    Serial.println(F("HELLO|VESTA_NANO|1"));
    return;
  }

  if (strcmp(command, "PING") == 0) {
    Serial.print(F("PONG|"));
    Serial.println(now);
    return;
  }

  if (strcmp(command, "STATUS") == 0) {
    printStatus();
    return;
  }

  if (strcmp(command, "READY") == 0) {
    deliveryError = false;
    acknowledge(F("READY"));
    return;
  }

  if (strcmp(command, "ALERT_RAISED") == 0) {
    char *alertId = strtok_r(NULL, "|", &savePointer);
    char *reason = strtok_r(NULL, "|", &savePointer);
    if (alertId == NULL || alertId[0] == '\0') {
      Serial.println(F("ERR|MISSING_ALERT_ID"));
      return;
    }

    copyText(activeAlertId, sizeof(activeAlertId), alertId);
    copyText(alertReason, sizeof(alertReason), reason == NULL ? "ALERTA" : reason);
    alertActive = true;
    deliveryError = false;
    resolvedDisplayUntil = 0;
    Serial.print(F("OK|ALERT_RAISED|"));
    Serial.println(activeAlertId);
    return;
  }

  if (strcmp(command, "ALERT_RESOLVED") == 0) {
    char *alertId = strtok_r(NULL, "|", &savePointer);
    if (alertId == NULL || alertId[0] == '\0') {
      Serial.println(F("ERR|MISSING_ALERT_ID"));
      return;
    }
    if (alertActive && activeAlertId[0] != '\0' && strcmp(alertId, activeAlertId) != 0) {
      Serial.println(F("ERR|ALERT_ID_MISMATCH"));
      return;
    }

    copyText(activeAlertId, sizeof(activeAlertId), alertId);
    alertActive = false;
    deliveryError = false;
    resolvedDisplayUntil = now + RESOLVED_DISPLAY_MS;
    startOneShot(SOUND_RESOLVED, now);
    Serial.print(F("OK|ALERT_RESOLVED|"));
    Serial.println(activeAlertId);
    return;
  }

  if (strcmp(command, "DELIVERY_FAILED") == 0) {
    deliveryError = true;
    startOneShot(SOUND_ERROR, now);
    acknowledge(F("DELIVERY_FAILED"));
    return;
  }

  if (strcmp(command, "DELIVERED") == 0) {
    deliveryError = false;
    deliveredLedUntil = now + DELIVERED_LED_MS;
    acknowledge(F("DELIVERED"));
    return;
  }

  if (strcmp(command, "CLEAR") == 0) {
    if (alertActive) {
      Serial.println(F("ERR|ALERT_ACTIVE"));
      return;
    }
    deliveryError = false;
    resolvedDisplayUntil = 0;
    deliveredLedUntil = 0;
    acknowledge(F("CLEAR"));
    return;
  }

  Serial.println(F("ERR|UNKNOWN_COMMAND"));
}

void readSerial(unsigned long now) {
  while (Serial.available() > 0) {
    const char incoming = (char)Serial.read();

    if (incoming == '\r') {
      continue;
    }

    if (incoming == '\n') {
      if (serialOverflow) {
        Serial.println(F("ERR|LINE_TOO_LONG"));
      } else if (serialLength > 0) {
        serialBuffer[serialLength] = '\0';
        processCommand(serialBuffer, now);
      }
      serialLength = 0;
      serialOverflow = false;
      continue;
    }

    if (serialOverflow) {
      continue;
    }

    if (serialLength < SERIAL_BUFFER_SIZE - 1) {
      serialBuffer[serialLength++] = incoming;
    } else {
      serialOverflow = true;
    }
  }
}

void readSensors(unsigned long now) {
  if (now - lastSensorReadAt < SENSOR_INTERVAL_MS) {
    return;
  }
  lastSensorReadAt = now;
  lightValue = analogRead(PIN_LIGHT);
  humidityValue = analogRead(PIN_HUMIDITY);
  joystickXValue = analogRead(PIN_JOYSTICK_X);
  joystickYValue = analogRead(PIN_JOYSTICK_Y);
}

void updateJoystick(unsigned long now) {
  const int reading = digitalRead(PIN_JOYSTICK_SWITCH);
  if (reading != lastButtonReading) {
    lastButtonReading = reading;
    buttonChangedAt = now;
  }

  if (reading != stableButtonState && now - buttonChangedAt >= BUTTON_DEBOUNCE_MS) {
    stableButtonState = reading;
    if (stableButtonState == LOW) {
      buttonDisplayUntil = now + BUTTON_DISPLAY_MS;
      startOneShot(SOUND_BUTTON, now);
      Serial.println(F("INPUT|JOYSTICK|PRESSED"));
    } else {
      Serial.println(F("INPUT|JOYSTICK|RELEASED"));
    }
  }
}

void updateLink(unsigned long now) {
  if (pcLinked && now - lastPcMessageAt > PC_TIMEOUT_MS) {
    pcLinked = false;
  }
}

void updateLeds(unsigned long now) {
  const bool fastBlink = ((now / 250UL) % 2UL) == 0;
  const bool slowBlink = ((now / 600UL) % 2UL) == 0;

  digitalWrite(LED_POWER, HIGH);
  digitalWrite(LED_LINK, pcLinked ? HIGH : slowBlink);
  digitalWrite(LED_DELIVERY_ERROR, deliveryError ? slowBlink : LOW);
  digitalWrite(LED_ALERT, alertActive ? fastBlink : LOW);
  digitalWrite(
    LED_RESOLVED,
    (deadlineActive(now, resolvedDisplayUntil) || deadlineActive(now, deliveredLedUntil)) ? HIGH : LOW
  );
}

void padLcdLine(char *destination, const char *source) {
  uint8_t index = 0;
  while (index < 16 && source[index] != '\0') {
    destination[index] = source[index];
    ++index;
  }
  while (index < 16) {
    destination[index++] = ' ';
  }
  destination[16] = '\0';
}

void writeLcdLine(uint8_t row, const char *text, char *lastText) {
  char padded[17];
  padLcdLine(padded, text);
  if (strcmp(padded, lastText) == 0) {
    return;
  }
  lcd.setCursor(0, row);
  lcd.print(padded);
  copyText(lastText, 17, padded);
}

void updateLcd(unsigned long now) {
  if (now - lastLcdUpdateAt < LCD_INTERVAL_MS) {
    return;
  }
  lastLcdUpdateAt = now;

  char line1[17];
  char line2[17];

  if (alertActive) {
    copyText(line1, sizeof(line1), "ALERTA ACTIVA");
    snprintf(line2, sizeof(line2), "%-16.16s", alertReason);
  } else if (deliveryError) {
    copyText(line1, sizeof(line1), "EROARE LIVRARE");
    copyText(line2, sizeof(line2), "Verifica PC");
  } else if (deadlineActive(now, resolvedDisplayUntil)) {
    copyText(line1, sizeof(line1), "ALERTA REZOLVATA");
    copyText(line2, sizeof(line2), "Dispatcher liber");
  } else if (!pcLinked) {
    copyText(line1, sizeof(line1), "ASTEPT PC");
    copyText(line2, sizeof(line2), "USB Serial");
  } else if (deadlineActive(now, buttonDisplayUntil)) {
    copyText(line1, sizeof(line1), "JOY APASAT");
    copyText(line2, sizeof(line2), "Trimis catre PC");
  } else {
    copyText(line1, sizeof(line1), "VESTA READY");
    snprintf(line2, sizeof(line2), "L:%4d U:%4d", lightValue, humidityValue);
  }

  writeLcdLine(0, line1, lastLcdLine1);
  writeLcdLine(1, line2, lastLcdLine2);
}

void setup() {
  Serial.begin(115200);

  lcd.init();
  lcd.backlight();

  for (uint8_t i = 0; i < sizeof(LEDS) / sizeof(LEDS[0]); ++i) {
    pinMode(LEDS[i], OUTPUT);
    digitalWrite(LEDS[i], LOW);
  }

  pinMode(BUZZER_ALERT, OUTPUT);
  pinMode(BUZZER_FEEDBACK, OUTPUT);
  pinMode(PIN_JOYSTICK_SWITCH, INPUT_PULLUP);

  digitalWrite(LED_POWER, HIGH);
  writeLcdLine(0, "VESTA DISP V1", lastLcdLine1);
  writeLcdLine(1, "ASTEPT PC", lastLcdLine2);
  Serial.println(F("BOOT|VESTA_NANO|1"));
}

void loop() {
  const unsigned long now = millis();

  readSerial(now);
  readSensors(now);
  updateJoystick(now);
  updateLink(now);
  updateLeds(now);
  updateSound(now);
  updateLcd(now);
}
