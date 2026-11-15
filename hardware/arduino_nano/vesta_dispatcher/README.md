# Vesta physical dispatcher — Arduino Nano

Sketch-ul funcționează pe Arduino Uno R3 sau Arduino Nano clasic (ATmega328P), conectat prin USB la PC. PC-ul gestionează Wi-Fi,
WebSocket-ul Vesta și autentificarea; placa primește numai comenzi seriale și controlează
LED-urile, buzzerele și LCD-ul. Nano nu are joystick, senzori sau conexiune Wi-Fi directă.

Programul de pe PC care face legătura cu backendul este în [`../bridge`](../bridge/README.md).

## Conectare

| Componentă | Pin Nano |
| --- | --- |
| LED alb — cod 00, standby | D2 |
| LED verde — cod 01, activ | D3 |
| LED galben — cod 02, verificare | D4 |
| LED roșu — cod 03, alertă | D5 |
| LED albastru — cod 04, eroare tehnică | D6 |
| Buzzer alertă rapidă | D7 |
| Buzzer eroare tehnică | D8 |
| D9–D13 | liberi |
| LCD I²C SDA/SCL | A4/A5 |

LCD-ul este configurat la adresa `0x27`, cu 16 coloane și 2 rânduri. Serial rulează la
`115200 baud`, 8N1. Fiecare comandă se termină cu newline (`\n`).

## Protocol PC → Nano

| Comandă | Efect |
| --- | --- |
| `HELLO` | Negociere; răspuns `HELLO\|VESTA_NANO\|1`. |
| `BYE` | PC-ul se oprește voit: placa trece în „Astept PC”, fără eroare. O alertă activă continuă să sune. |
| `PING` | Menține legătura; răspuns `PONG\|<millis>`. Se trimite la 2–5 secunde. |
| `STATUS` | Returnează starea conexiunii, starea vizuală și alerta curentă. |
| `READY` | Confirmă legătura cu PC-ul și șterge eroarea tehnică explicită, fără să închidă o alertă. |
| `STATE\|STANDBY` | LED alb; LCD: sistem pregătit, fără sesiune. |
| `STATE\|ACTIVE` | LED verde; LCD: conversație activă și funcțională. |
| `STATE\|CHECK` | LED galben intermitent; LCD: se așteaptă confirmarea. |
| `ALERT_RAISED\|<alert_id>\|<reason>` | Activează LED-ul și alarma repetitivă. |
| `ALERT_RESOLVED\|<alert_id>` | Oprește alerta numai dacă ID-ul corespunde. |
| `TECHNICAL_ERROR\|<reason>` | LED albastru și două semnale sonore lungi pe buzzerul D8. |
| `TECHNICAL_RESOLVED` | Închide eroarea tehnică explicită. |
| `DELIVERY_FAILED\|<notification_id>` | Alias de eroare tehnică pentru livrare. |
| `DELIVERED\|<notification_id>` | Închide eroarea de livrare. |
| `CLEAR` | Șterge mesajele temporare; este refuzat cât timp există o alertă activă. |

Exemple:

```text
PING
STATE|ACTIVE
STATE|CHECK
ALERT_RAISED|550e8400-e29b-41d4-a716-446655440000|SOS_EXPLICIT
ALERT_RESOLVED|550e8400-e29b-41d4-a716-446655440000
TECHNICAL_ERROR|MICROFON
TECHNICAL_RESOLVED
```

## Protocol Nano → PC

- `BOOT|VESTA_NANO|1`
- `OK|<comandă>` sau `ERR|<motiv>`
- răspunsurile `PONG`, `STATUS` (`...|TECH=0|LCD=1|ID=`) și `HELLO`
- `LINK|LOST|IDLE` (PC tăcut 10 s în repaus: doar așteptare) sau `LINK|LOST|SESSION` (PC tăcut în timpul unei sesiuni sau alerte: eroare)
- `LCD|MISSING` la pornire, `LCD|LOST` / `LCD|OK` când LCD-ul cade sau revine

## Comportament de siguranță

- Nu există apeluri blocante `delay()`.
- Placa știe că „are PC” doar din comenzile primite; conectarea USB singură nu se vede.
  Fără comenzi timp de 10 secunde:
  - **în repaus** (fără sesiune și fără alertă) placa doar așteaptă: LED alb cu o scurtă
    pauză la 2 s, LCD `ALB 00 STANDBY / Astept PC`, fără buzzer. Erorile tehnice vechi se șterg;
  - **în timpul unei sesiuni sau alerte** apare eroarea `ALBASTRU 04 ERR / CONEXIUNE PC` cu
    buzzerul D8, fiindcă starea afișată poate fi depășită.
- Dacă placa se resetează în mijlocul unei scrieri pe LCD, modulul I²C poate ține SDA jos.
  La pornire placa eliberează magistrala (impulsuri pe SCL), iar fiecare tranzacție I²C are
  timeout de 3 ms: un LCD deconectat nu mai blochează LED-urile, buzzerele sau serialul.
  LCD-ul se reîncearcă la 5 s.
- O alertă activă continuă să sune chiar dacă legătura cu PC-ul cade.
- Un `ALERT_RESOLVED` cu alt `alert_id` nu poate opri alarma curentă.
- Identificatorul notificării permite deduplicarea în bridge-ul care va rula pe PC.
- Dacă alerta și eroarea tehnică există simultan, alerta roșie și buzzerul D7 au prioritate;
  eroarea albastră reapare după rezolvarea alertei.

## Mesaje LCD

| Situație | Linia 1 | Linia 2 |
| --- | --- | --- |
| Standby | `ALB 00 STANDBY` | `Fara sesiune` |
| PC tăcut, în repaus (sau după `BYE`) | `ALB 00 STANDBY` | `Astept PC` |
| Activ | `VERDE 01 ACTIV` | `Conversatie OK` |
| Verificare | `GALBEN 02 CHECK` | `Astept raspuns` |
| Alertă | `ROSU 03 ALERTA` | motivul alertei |
| Eroare tehnică | `ALBASTRU 04 ERR` | componenta sau `CONEXIUNE PC` (doar în sesiune/alertă) |
| Alertă rezolvată | `ALB 00 STANDBY` | `Alerta rezolvata` |

Motivele backendului sunt adaptate pentru cele 16 caractere ale LCD-ului: `SOS_EXPLICIT`
devine `AJUTOR SOLICITAT`, timeout-ul devine `FARA CONFIRMARE`, iar erorile cunoscute sunt
afișate ca `CONEXIUNE`, `MICROFON`, `AUDIO`, `SERVICIU VOCAL` sau `ALIMENTARE`.

Pe ATmega328P, funcția `tone()` poate genera un singur ton la un moment dat. Din acest motiv,
buzzerul D7 are prioritate cât timp există o alertă, iar D8 semnalizează eroarea tehnică după
închiderea alertei.
