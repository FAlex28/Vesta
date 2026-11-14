# Vesta physical dispatcher — Arduino Nano

Sketch-ul presupune un Arduino Nano clasic conectat prin USB la PC. PC-ul gestionează Wi-Fi,
WebSocket-ul Vesta și autentificarea; placa primește numai comenzi seriale și controlează
componentele fizice.

## Conectare

| Componentă | Pin Nano |
| --- | --- |
| LED power | D2 |
| LED legătură PC | D3 |
| LED eroare livrare | D4 |
| LED alertă | D5 |
| LED rezolvare/livrare | D6 |
| Buzzer alertă | D7 |
| Buzzer feedback | D8 |
| Joystick SW | D9, cu `INPUT_PULLUP` |
| Senzor lumină | A0 |
| Senzor umiditate | A1 |
| Joystick X/Y | A2/A3 |
| LCD I²C SDA/SCL | A4/A5 |

LCD-ul este configurat la adresa `0x27`, cu 16 coloane și 2 rânduri. Serial rulează la
`115200 baud`, 8N1. Fiecare comandă se termină cu newline (`\n`).

## Protocol PC → Nano

| Comandă | Efect |
| --- | --- |
| `HELLO` | Negociere; răspuns `HELLO\|VESTA_NANO\|1`. |
| `PING` | Menține legătura; răspuns `PONG\|<millis>`. Se trimite la 2–5 secunde. |
| `STATUS` | Returnează starea plăcii și valorile brute ale senzorilor. |
| `READY` | Șterge eroarea de livrare, fără să închidă o alertă. |
| `ALERT_RAISED\|<alert_id>\|<reason>` | Activează LED-ul și alarma repetitivă. |
| `ALERT_RESOLVED\|<alert_id>` | Oprește alerta numai dacă ID-ul corespunde. |
| `DELIVERY_FAILED\|<notification_id>` | Semnalizează o problemă de livrare. |
| `DELIVERED\|<notification_id>` | Șterge eroarea și aprinde temporar LED-ul de confirmare. |
| `CLEAR` | Șterge mesajele temporare; este refuzat cât timp există o alertă activă. |

Exemple:

```text
PING
ALERT_RAISED|550e8400-e29b-41d4-a716-446655440000|SOS_EXPLICIT
ALERT_RESOLVED|550e8400-e29b-41d4-a716-446655440000
```

## Protocol Nano → PC

- `BOOT|VESTA_NANO|1`
- `OK|<comandă>` sau `ERR|<motiv>`
- `INPUT|JOYSTICK|PRESSED`
- `INPUT|JOYSTICK|RELEASED`
- răspunsurile `PONG`, `STATUS` și `HELLO`

Apăsarea joystickului este doar raportată PC-ului. Nu rezolvă local o alertă; decizia trebuie
validată și persistată de backend.

## Comportament de siguranță

- Nu există apeluri blocante `delay()`.
- Dacă PC-ul nu mai trimite comenzi timp de 10 secunde, legătura este indicată ca offline.
- O alertă activă continuă să sune chiar dacă legătura cu PC-ul cade.
- Un `ALERT_RESOLVED` cu alt `alert_id` nu poate opri alarma curentă.
- Identificatorul notificării permite deduplicarea în bridge-ul care va rula pe PC.
