#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <string.h>

// Vesta physical dispatcher for an Arduino Nano (ATmega328P).
// The PC owns Wi-Fi and sends newline-terminated commands over USB Serial.

LiquidCrystal_I2C lcd(0x27, 16, 2);

// Standard vizual Vesta: 00 alb, 01 verde, 02 galben, 03 roșu, 04 albastru.
const uint8_t LED_STANDBY_WHITE = 2;
const uint8_t LED_ACTIVE_GREEN = 3;
const uint8_t LED_CHECK_YELLOW = 4;
const uint8_t LED_ALERT_RED = 5;
const uint8_t LED_TECHNICAL_BLUE = 6;
const uint8_t LEDS[] = {
  LED_STANDBY_WHITE,
  LED_ACTIVE_GREEN,
  LED_CHECK_YELLOW,
  LED_ALERT_RED,
  LED_TECHNICAL_BLUE,
};

// Cablajul existent se păstrează: buzzerele sunt pe D7 și D8.
const uint8_t BUZZER_ALERT = 7;
const uint8_t BUZZER_TECHNICAL = 8;

const unsigned long PC_TIMEOUT_MS = 10000UL;
const unsigned long LCD_INTERVAL_MS = 150UL;
const unsigned long RESOLVED_DISPLAY_MS = 5000UL;

const uint8_t SERIAL_BUFFER_SIZE = 120;
char serialBuffer[SERIAL_BUFFER_SIZE];
uint8_t serialLength = 0;
bool serialOverflow = false;

bool pcLinked = false;
bool alertActive = false;
bool technicalError = false;
unsigned long lastPcMessageAt = 0;
unsigned long resolvedDisplayUntil = 0;
unsigned long technicalSoundStartedAt = 0;

char activeAlertId[40] = "";
char alertReason[24] = "";
char technicalReason[24] = "";

const uint8_t STATE_STANDBY = 0;
const uint8_t STATE_ACTIVE = 1;
const uint8_t STATE_CHECK = 2;
uint8_t safetyState = STATE_STANDBY;

unsigned long lastLcdUpdateAt = 0;
char lastLcdLine1[17] = "";
char lastLcdLine2[17] = "";

const uint8_t SOUND_NONE = 0;
const uint8_t SOUND_RESOLVED = 1;

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

void setAlertReasonText(const char *reason) {
  if (reason == NULL) {
    copyText(alertReason, sizeof(alertReason), "ALERTA");
  } else if (strcmp(reason, "SOS_EXPLICIT") == 0) {
    copyText(alertReason, sizeof(alertReason), "AJUTOR SOLICITAT");
  } else if (strcmp(reason, "MISSED_CHECK_INS") == 0 ||
             strcmp(reason, "CHECKIN_TIMEOUT") == 0) {
    copyText(alertReason, sizeof(alertReason), "FARA CONFIRMARE");
  } else {
    copyText(alertReason, sizeof(alertReason), reason);
  }
}

void setTechnicalReasonText(const char *reason) {
  if (reason == NULL) {
    copyText(technicalReason, sizeof(technicalReason), "EROARE TEHNICA");
  } else if (strcmp(reason, "NETWORK_DISCONNECTED") == 0) {
    copyText(technicalReason, sizeof(technicalReason), "CONEXIUNE");
  } else if (strcmp(reason, "MICROPHONE_DENIED") == 0) {
    copyText(technicalReason, sizeof(technicalReason), "MICROFON");
  } else if (strcmp(reason, "AUDIO_INTERRUPTED") == 0 ||
             strcmp(reason, "AUDIO_DISCONNECTED") == 0) {
    copyText(technicalReason, sizeof(technicalReason), "AUDIO");
  } else if (strcmp(reason, "VOICE_UNAVAILABLE") == 0 ||
             strcmp(reason, "VOICE_TIMEOUT") == 0) {
    copyText(technicalReason, sizeof(technicalReason), "SERVICIU VOCAL");
  } else if (strcmp(reason, "LOW_VOLTAGE") == 0 ||
             strcmp(reason, "POWER_UNSTABLE") == 0) {
    copyText(technicalReason, sizeof(technicalReason), "ALIMENTARE");
  } else {
    copyText(technicalReason, sizeof(technicalReason), reason);
  }
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
      setToneOutput(BUZZER_TECHNICAL, 1500);
    } else if (elapsed < 220UL) {
      stopToneOutput();
    } else if (elapsed < 360UL) {
      setToneOutput(BUZZER_TECHNICAL, 1900);
    } else {
      oneShotSound = SOUND_NONE;
      stopToneOutput();
    }
    return;
  }

}

void updateAlertSound(unsigned long now) {
  // D7: Bip - Bip - Bip rapid, apoi o pauză scurtă.
  const unsigned long phase = now % 1200UL;

  if (phase < 150UL) {
    setToneOutput(BUZZER_ALERT, 1200);
  } else if (phase < 250UL) {
    stopToneOutput();
  } else if (phase < 400UL) {
    setToneOutput(BUZZER_ALERT, 1200);
  } else if (phase < 500UL) {
    stopToneOutput();
  } else if (phase < 650UL) {
    setToneOutput(BUZZER_ALERT, 1600);
  } else {
    stopToneOutput();
  }
}

void updateTechnicalSound(unsigned long now) {
  // D8: Biiiip - pauză - Biiiip, repetat cât timp eroarea persistă.
  const unsigned long phase = (now - technicalSoundStartedAt) % 4200UL;

  if (phase < 900UL) {
    setToneOutput(BUZZER_TECHNICAL, 650);
  } else if (phase < 1500UL) {
    stopToneOutput();
  } else if (phase < 2400UL) {
    setToneOutput(BUZZER_TECHNICAL, 650);
  } else {
    stopToneOutput();
  }
}

bool technicalStateActive() {
  return technicalError || !pcLinked;
}

void updateSound(unsigned long now) {
  if (oneShotSound != SOUND_NONE) {
    updateOneShotSound(now);
  } else if (alertActive) {
    updateAlertSound(now);
  } else if (technicalStateActive()) {
    updateTechnicalSound(now);
  } else {
    stopToneOutput();
  }
}

void markPcActivity(unsigned long now) {
  pcLinked = true;
  lastPcMessageAt = now;
}

const char *safetyStateName() {
  if (alertActive) {
    return "ALERT";
  }
  if (safetyState == STATE_ACTIVE) {
    return "ACTIVE";
  }
  if (safetyState == STATE_CHECK) {
    return "CHECK";
  }
  return "STANDBY";
}

bool applySafetyState(const char *state) {
  if (strcmp(state, "STANDBY") == 0 || strcmp(state, "IDLE") == 0 ||
      strcmp(state, "CANCELLED") == 0) {
    safetyState = STATE_STANDBY;
    return true;
  }
  if (strcmp(state, "ACTIVE") == 0) {
    safetyState = STATE_ACTIVE;
    return true;
  }
  if (strcmp(state, "CHECK") == 0 || strcmp(state, "CHECK_IN") == 0 ||
      strcmp(state, "UNCONFIRMED") == 0) {
    safetyState = STATE_CHECK;
    return true;
  }
  return false;
}

void printStatus() {
  Serial.print(F("STATUS|PC="));
  Serial.print(pcLinked ? 1 : 0);
  Serial.print(F("|STATE="));
  Serial.print(safetyStateName());
  Serial.print(F("|ALERT="));
  Serial.print(alertActive ? 1 : 0);
  Serial.print(F("|TECH="));
  Serial.print(technicalStateActive() ? 1 : 0);
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
    technicalError = false;
    acknowledge(F("READY"));
    return;
  }

  if (strcmp(command, "STATE") == 0) {
    char *state = strtok_r(NULL, "|", &savePointer);
    if (state == NULL || state[0] == '\0') {
      Serial.println(F("ERR|MISSING_STATE"));
      return;
    }
    if (alertActive) {
      Serial.println(F("ERR|ALERT_ACTIVE"));
      return;
    }
    if (!applySafetyState(state)) {
      Serial.println(F("ERR|UNKNOWN_STATE"));
      return;
    }
    Serial.print(F("OK|STATE|"));
    Serial.println(safetyStateName());
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
    setAlertReasonText(reason);
    alertActive = true;
    safetyState = STATE_STANDBY;
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
    safetyState = STATE_STANDBY;
    resolvedDisplayUntil = now + RESOLVED_DISPLAY_MS;
    startOneShot(SOUND_RESOLVED, now);
    Serial.print(F("OK|ALERT_RESOLVED|"));
    Serial.println(activeAlertId);
    return;
  }

  if (strcmp(command, "TECHNICAL_ERROR") == 0 || strcmp(command, "DELIVERY_FAILED") == 0) {
    char *reason = strtok_r(NULL, "|", &savePointer);
    technicalError = true;
    technicalSoundStartedAt = now;
    if (strcmp(command, "DELIVERY_FAILED") == 0) {
      copyText(technicalReason, sizeof(technicalReason), "LIVRARE ESUATA");
    } else {
      setTechnicalReasonText(reason);
    }
    Serial.print(F("OK|"));
    Serial.println(command);
    return;
  }

  if (strcmp(command, "TECHNICAL_RESOLVED") == 0 || strcmp(command, "DELIVERED") == 0) {
    technicalError = false;
    technicalReason[0] = '\0';
    Serial.print(F("OK|"));
    Serial.println(command);
    return;
  }

  if (strcmp(command, "CLEAR") == 0) {
    if (alertActive) {
      Serial.println(F("ERR|ALERT_ACTIVE"));
      return;
    }
    technicalError = false;
    technicalReason[0] = '\0';
    resolvedDisplayUntil = 0;
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

void updateLink(unsigned long now) {
  if (pcLinked && now - lastPcMessageAt > PC_TIMEOUT_MS) {
    pcLinked = false;
    technicalSoundStartedAt = now;
  }
}

void updateLeds(unsigned long now) {
  const bool fastBlink = ((now / 250UL) % 2UL) == 0;
  const bool slowBlink = ((now / 600UL) % 2UL) == 0;

  for (uint8_t i = 0; i < sizeof(LEDS) / sizeof(LEDS[0]); ++i) {
    digitalWrite(LEDS[i], LOW);
  }

  // Prioritate: alertă > eroare tehnică > verificare > activ > standby.
  if (alertActive) {
    digitalWrite(LED_ALERT_RED, fastBlink);
  } else if (technicalStateActive()) {
    digitalWrite(LED_TECHNICAL_BLUE, slowBlink);
  } else if (safetyState == STATE_CHECK) {
    digitalWrite(LED_CHECK_YELLOW, slowBlink);
  } else if (safetyState == STATE_ACTIVE) {
    digitalWrite(LED_ACTIVE_GREEN, HIGH);
  } else {
    digitalWrite(LED_STANDBY_WHITE, HIGH);
  }
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
    copyText(line1, sizeof(line1), "ROSU 03 ALERTA");
    snprintf(line2, sizeof(line2), "%-16.16s", alertReason);
  } else if (technicalStateActive()) {
    copyText(line1, sizeof(line1), "ALBASTRU 04 ERR");
    copyText(
      line2,
      sizeof(line2),
      pcLinked ? (technicalReason[0] == '\0' ? "EROARE TEHNICA" : technicalReason) : "CONEXIUNE PC"
    );
  } else if (deadlineActive(now, resolvedDisplayUntil)) {
    copyText(line1, sizeof(line1), "ALB 00 STANDBY");
    copyText(line2, sizeof(line2), "Alerta rezolvata");
  } else if (safetyState == STATE_CHECK) {
    copyText(line1, sizeof(line1), "GALBEN 02 CHECK");
    copyText(line2, sizeof(line2), "Astept raspuns");
  } else if (safetyState == STATE_ACTIVE) {
    copyText(line1, sizeof(line1), "VERDE 01 ACTIV");
    copyText(line2, sizeof(line2), "Conversatie OK");
  } else {
    copyText(line1, sizeof(line1), "ALB 00 STANDBY");
    copyText(line2, sizeof(line2), "Fara sesiune");
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
  pinMode(BUZZER_TECHNICAL, OUTPUT);

  writeLcdLine(0, "VESTA DISP V1", lastLcdLine1);
  writeLcdLine(1, "Conectare PC", lastLcdLine2);
  Serial.println(F("BOOT|VESTA_NANO|1"));
}

void loop() {
  const unsigned long now = millis();

  readSerial(now);
  updateLink(now);
  updateLeds(now);
  updateSound(now);
  updateLcd(now);
}
