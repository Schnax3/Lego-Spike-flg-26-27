"""
====================================================================
 MOTOR-WIEDERGABE-PROGRAMM fuer LEGO Education SPIKE App (3.6.1)
====================================================================

Gegenstueck zum Aufzeichnungs-Programm. Es liest die dort ausgegebene
Kodierung ein und faehrt die Route / Bewegungen wieder ab.

Aufbau der Aufzeichnung (vom Recorder):
    PORTS|BASE|frame1|frame2|...
    Frame: <zeit_delta_ms>;<port><delta_grad><port><delta_grad>...
    (alle Zahlen in Basis BASE, negative mit "-" davor)

Wie benutzt man es?
--------------------
1. Die Ausgabe des Recorders (die lange Zeile zwischen
   "=== AUFZEICHNUNG ..." und "=== ENDE ===") unten bei REC_DATA
   zwischen die Anfuehrungszeichen einfuegen.
2. Roboter an die gleiche Startposition wie bei der Aufzeichnung stellen
   (Greifer in gleicher Stellung!).
3. Programm starten. Linker Button = Start, Rechter Button = Abbruch.

Wie funktioniert die Wiedergabe?
---------------------------------
Es werden KEINE Sensoren gebraucht. Jeder Motor hat einen eingebauten
Drehgeber (Gradzaehler). Die Wiedergabe arbeitet als einfache Regelung:
- Aus der Aufzeichnung ergibt sich zu jedem Zeitpunkt eine Soll-Position
  (zwischen den Frames wird linear interpoliert).
- Die Soll-Geschwindigkeit der Aufzeichnung wird als Vorsteuerung genutzt.
- Zusaetzlich korrigiert ein P-Regler jede Abweichung zwischen Soll- und
  Ist-Position. So gleichen sich Akkustand, Reibung usw. selbst aus.
"""

from spike import PrimeHub, Motor
from spike.control import wait_for_seconds
import utime


# ====================================================================
# KONFIGURATION
# ====================================================================

# Hier die Ausgabe des Aufzeichnungs-Programms einfuegen:
REC_DATA = "A,B|36|"

# Wiedergabe-Tempo: 1.0 = wie aufgezeichnet, 0.5 = halb so schnell
SPEED_FACTOR = 1.0

# Reglerstaerke: Prozent Motorleistung pro Grad Abweichung.
# Zu klein = Roboter hinkt hinterher, zu gross = Ruckeln / Schwingen.
KP = 0.8

# Motorleistung (Prozent) bei der der Motor etwa diese Grad pro Sekunde
# schafft. Medium-Motor ca. 1000-1100, Large-Motor ca. 900-1000.
# Bei Bedarf feintunen (Roboter zu langsam -> Wert verkleinern).
DEG_PER_SEC_AT_100 = 1000

# Maximale Motorleistung pro Port (Prozent). Ports die hier nicht
# stehen, nutzen DEFAULT_MAX_SPEED. Greifer bewusst sanfter, damit sie
# bei Widerstand nicht zu fest druecken.
DEFAULT_MAX_SPEED = 100
MAX_SPEED = {"A": 100, "B": 100, "E": 50, "F": 50}

# Was der Motor am Ende / bei Abbruch tun soll: "brake", "hold" oder "coast".
# Greifer sollen halten, Fahrmotoren bremsen.
DEFAULT_STOP_ACTION = "brake"
STOP_ACTION = {"A": "brake", "B": "brake", "E": "hold", "F": "hold"}

# Regelintervall in Millisekunden
CONTROL_INTERVAL_MS = 20

# Nach dem letzten Frame noch bis zu so lange nachregeln, bis alle
# Motoren nahe genug am Ziel sind (Millisekunden / Grad Toleranz).
SETTLE_TIMEOUT_MS = 1500
SETTLE_TOLERANCE_DEG = 3


# ====================================================================
# AB HIER: Programmlogik (normalerweise nicht aendern)
# ====================================================================

hub = PrimeHub()

PORT_LETTERS = "ABCDEF"


def decode_number(text, base):
    """Gegenstueck zu encode_number des Recorders (Basis-N, "-" = negativ)."""
    if text == "" or text == "0":
        return 0
    negative = text[0] == "-"
    if negative:
        text = text[1:]
    value = int(text, base)
    if negative:
        value = -value
    return value


def parse_recording(data):
    """
    Zerlegt die Aufzeichnung in:
        ports   - Liste der Port-Buchstaben
        times   - Liste kumulierter Zeitpunkte in ms (beginnt bei 0)
        targets - Dict: Port -> Liste kumulierter Positionen relativ zum
                  Start (gleich lang wie times, beginnt bei 0)
    """
    parts = data.strip().split("|")
    if len(parts) < 3:
        raise ValueError("REC_DATA ist leer oder unvollstaendig")

    ports = parts[0].split(",")
    base = int(parts[1])

    times = [0]
    targets = {}
    for p in ports:
        targets[p] = [0]

    for frame in parts[2:]:
        if frame == "":
            continue
        time_text, rest = frame.split(";")
        dt = decode_number(time_text, base)

        # Port-Abschnitte lesen: Grossbuchstabe A-F beginnt einen neuen
        # Abschnitt, die Zahl besteht aus Ziffern/Kleinbuchstaben/"-".
        deltas = {}
        i = 0
        while i < len(rest):
            port_letter = rest[i]
            j = i + 1
            while j < len(rest) and rest[j] not in PORT_LETTERS:
                j += 1
            deltas[port_letter] = decode_number(rest[i + 1:j], base)
            i = j

        times.append(times[-1] + dt)
        for p in ports:
            targets[p].append(targets[p][-1] + deltas.get(p, 0))

    return ports, times, targets


def clamp(value, limit):
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


def stop_all(motors):
    for p in motors:
        motors[p].stop()


def replay():
    ports, times, targets = parse_recording(REC_DATA)
    if len(times) < 2:
        print("Keine Frames in REC_DATA gefunden.")
        return

    motors = {}
    start_pos = {}
    for p in ports:
        m = Motor(p)
        m.set_stall_detection(False)
        m.set_stop_action(STOP_ACTION.get(p, DEFAULT_STOP_ACTION))
        motors[p] = m

    print("Ports:", ports, "Frames:", len(times) - 1,
          "Dauer (s):", times[-1] / 1000)

    hub.light_matrix.show_image("SQUARE")
    print("Warte auf linken Button (Start)...")
    while not hub.left_button.was_pressed():
        wait_for_seconds(0.05)

    hub.light_matrix.show_image("ARROW_E")
    print("Wiedergabe gestartet.")

    # Startposition merken: alle Soll-Positionen sind relativ dazu
    for p in ports:
        start_pos[p] = motors[p].get_degrees_counted()

    # Rechten Button vorher leeren, damit alte Klicks nicht sofort abbrechen
    hub.right_button.was_pressed()

    total_ms = times[-1]
    start_time = utime.ticks_ms()
    segment = 0
    aborted = False
    finished_at = None

    while True:
        if hub.right_button.was_pressed():
            aborted = True
            break

        elapsed = utime.ticks_diff(utime.ticks_ms(), start_time)
        t = elapsed * SPEED_FACTOR  # Position in der Aufzeichnungs-Zeit

        in_recording = t < total_ms

        if in_recording:
            # passendes Segment [times[segment], times[segment+1]] suchen
            while segment < len(times) - 2 and t >= times[segment + 1]:
                segment += 1
            t0 = times[segment]
            t1 = times[segment + 1]
            span = t1 - t0
            if span > 0:
                frac = (t - t0) / span
            else:
                frac = 1.0
        else:
            segment = len(times) - 2
            t0 = times[segment]
            t1 = times[segment + 1]
            span = t1 - t0
            frac = 1.0

        all_close = True
        for p in ports:
            p0 = targets[p][segment]
            p1 = targets[p][segment + 1]
            target = start_pos[p] + p0 + (p1 - p0) * frac
            actual = motors[p].get_degrees_counted()
            error = target - actual

            if in_recording and span > 0:
                # Vorsteuerung: aufgezeichnete Geschwindigkeit (Grad/s)
                feed_deg_s = (p1 - p0) * 1000.0 / span * SPEED_FACTOR
                feed = feed_deg_s / DEG_PER_SEC_AT_100 * 100.0
            else:
                feed = 0

            power = feed + KP * error
            limit = MAX_SPEED.get(p, DEFAULT_MAX_SPEED)
            power = clamp(power, limit)

            if abs(error) > SETTLE_TOLERANCE_DEG:
                all_close = False

            motors[p].start(int(power))

        if not in_recording:
            if finished_at is None:
                finished_at = utime.ticks_ms()
            waited = utime.ticks_diff(utime.ticks_ms(), finished_at)
            if all_close or waited > SETTLE_TIMEOUT_MS:
                break

        wait_for_seconds(CONTROL_INTERVAL_MS / 1000)

    stop_all(motors)

    if aborted:
        hub.light_matrix.show_image("NO")
        print("Wiedergabe abgebrochen.")
    else:
        hub.light_matrix.show_image("YES")
        print("Wiedergabe fertig.")
        for p in ports:
            ziel = start_pos[p] + targets[p][-1]
            ist = motors[p].get_degrees_counted()
            print("Port", p, "Ziel:", ziel, "Ist:", ist, "Abweichung:", ziel - ist)


def main():
    replay()


main()
