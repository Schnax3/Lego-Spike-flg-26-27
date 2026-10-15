"""
====================================================================
 WIEDERGABE-PROGRAMM fuer LEGO Education SPIKE App (3.6.1)
 (Version mit Regelung)
====================================================================

Was macht dieses Programm?
---------------------------
Es liest die eigene, kompakte Kodierung (erzeugt vom
Aufzeichnungs-Programm "motor_recorder.py") aus einer Variable
ein und spielt die aufgezeichnete Motor-Bewegung auf dem Hub ab.

Format der Kodierung (zur Erinnerung):
    <PortA>,<PortB>,...|<Base>|<Frame1>|<Frame2>|...
    Jeder Frame: <zeit_delta_base_n>;<Port><delta_pos><Port><delta_pos>...

Was ist neu gegenueber der alten Version?
------------------------------------------
Alt: Pro Frame wurden die Motoren gestartet und gestoppt. Dadurch
ruckelte die Fahrt, und jeder kleine Fehler (Schlupf, Auslaufen,
Akkustand) hat sich auf alle folgenden Frames aufaddiert. Ausserdem
konnte ein blockierter Greifer das Programm endlos haengen lassen.

Neu: Alle Frames werden zu einer Zeitleiste mit ABSOLUTEN Soll-Positionen
(relativ zur Startposition) zusammengerechnet. Waehrend der Wiedergabe
laeuft eine Regelung fuer jeden Motor gleichzeitig:
- Soll-Position = zwischen den Frames linear interpoliert
- Vorsteuerung = aufgezeichnete Geschwindigkeit
- P-Regler = korrigiert jede Abweichung von der Soll-Position
Es werden keine Sensoren gebraucht, nur die Drehgeber der Motoren.

Bedienung:
    Linker Button  = Start
    Rechter Button = Abbruch (stoppt alle Motoren)
Vor dem Start den Roboter in dieselbe Startposition wie bei der
Aufzeichnung stellen (auch die Greifer!).
"""

from spike import PrimeHub, Motor
from spike.control import wait_for_seconds
import utime


# ====================================================================
# KONFIGURATION -- hier alles einstellen, ohne den Rest anzufassen
# ====================================================================

# Hier die Ausgabe des Aufzeichnungs-Programms einfuegen (kompletter
# String zwischen den Zeilen "=== AUFZEICHNUNG ===" und "=== ENDE ===").
# Beispiel-Platzhalter unten -> durch deine echte Aufzeichnung ersetzen!
ENCODED_RECORDING = "A,B|36|a;A50Bb|5;A20B-5|a;A0B10"

# Wiedergabe-Tempo: 1.0 = wie aufgezeichnet, 0.5 = halb so schnell
# (zum Testen praktisch).
SPEED_FACTOR = 1.0

# Reglerstaerke: Prozent Motorleistung pro Grad Abweichung.
# Hinkt der Roboter hinterher -> erhoehen. Ruckelt/schwingt er -> senken.
KP = 0.8

# Bei welcher Motorleistung (100 %) schafft der Motor ungefaehr wie
# viele Grad pro Sekunde? Medium ca. 1000-1100, Large ca. 900-1000.
# Ist der Roboter zu langsam -> Wert verkleinern, zu schnell -> vergroessern.
DEG_PER_SEC_AT_100 = 1000

# Maximale Motorleistung pro Port (0-100). Ports ohne Eintrag nutzen
# DEFAULT_MAX_SPEED. Greifer (E, F) bewusst sanfter, damit sie bei
# Widerstand nicht zu fest druecken.
DEFAULT_MAX_SPEED = 100
MAX_SPEED = {"A": 100, "B": 100, "E": 50, "F": 50}

# Verhalten am Ende / bei Abbruch: "brake", "hold" oder "coast".
# Fahrmotoren bremsen, Greifer halten ihre Position.
DEFAULT_STOP_ACTION = "brake"
STOP_ACTION = {"A": "brake", "B": "brake", "E": "hold", "F": "hold"}

# Wie oft die Regelung laeuft (Millisekunden zwischen zwei Durchlaeufen)
CONTROL_INTERVAL_MS = 20

# Nach dem letzten Frame so lange (max.) nachregeln, bis alle Motoren
# nah genug am Ziel sind. Danach wird in jedem Fall gestoppt.
SETTLE_TIMEOUT_MS = 1500
SETTLE_TOLERANCE_DEG = 3

# Kodierungs-Basis, MUSS zur Aufzeichnung passen (steht auch im
# Header von ENCODED_RECORDING selbst und wird von dort gelesen -
# diese Variable hier dient nur als Rueckfall/Kontrolle).
DEFAULT_ENCODING_BASE = 36


# ====================================================================
# AB HIER: Programmlogik (normalerweise nicht aendern)
# ====================================================================

hub = PrimeHub()


def decode_number(text, base):
    """
    Kehrt encode_number() aus dem Aufzeichnungs-Programm um:
    wandelt eine Base-N-Zeichenkette (mit optionalem "-" Praefix)
    zurueck in eine Ganzzahl.
    """
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    if text == "":
        return 0

    negative = text.startswith("-")
    if negative:
        text = text[1:]

    value = 0
    for char in text:
        value = value * base + digits.index(char)

    return -value if negative else value


def parse_frame(frame_text, ports, base):
    """
    Zerlegt einen Frame-String ("<zeit>;<Port><delta><Port><delta>...")
    in: (zeit_delta_ms, {port: delta_position, ...})
    """
    time_part, ports_part = frame_text.split(";")
    time_delta = decode_number(time_part, base)

    deltas = {}
    remaining = ports_part
    for port_letter in ports:
        if not remaining.startswith(port_letter):
            # ANNAHME: jeder Port taucht in jedem Frame genau einmal auf,
            # in derselben Reihenfolge wie im Header. Fehlt ein Port,
            # wird sein Delta als 0 gewertet.
            deltas[port_letter] = 0
            continue

        remaining = remaining[1:]  # Port-Buchstabe entfernen

        # Zahl bis zum naechsten Port-Buchstaben oder Ende einlesen
        number_text = ""
        while remaining and remaining[0] not in ports:
            number_text += remaining[0]
            remaining = remaining[1:]

        deltas[port_letter] = decode_number(number_text, base)

    return time_delta, deltas


def parse_recording(encoded_text):
    """
    Zerlegt die komplette Kodierung in:
    (ports_liste, base, liste_von_frames)
    """
    parts = encoded_text.strip().split("|")
    header_ports = parts[0]
    header_base = int(parts[1])
    frame_texts = parts[2:]

    ports = header_ports.split(",")

    frames = []
    for frame_text in frame_texts:
        if frame_text == "":
            continue
        frames.append(parse_frame(frame_text, ports, header_base))

    return ports, header_base, frames


def build_timeline(ports, frames):
    """
    Rechnet die Frame-Deltas zu einer Zeitleiste mit kumulierten Werten um:
        times   - Liste von Zeitpunkten in ms (beginnt bei 0)
        targets - Dict: Port -> Liste kumulierter Positionen (Grad,
                  relativ zur Startposition, beginnt bei 0)
    """
    times = [0]
    targets = {}
    for port_letter in ports:
        targets[port_letter] = [0]

    for time_delta_ms, deltas in frames:
        times.append(times[-1] + time_delta_ms)
        for port_letter in ports:
            targets[port_letter].append(targets[port_letter][-1] + deltas[port_letter])

    return times, targets


def clamp(value, limit):
    """Begrenzt value auf den Bereich -limit .. +limit."""
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


def stop_all(motors):
    """Stoppt alle Motoren (mit der je Port eingestellten Stopp-Aktion)."""
    for port_letter in motors:
        motors[port_letter].stop()


def run_motors_simultaneously(motors, ports, times, targets, start_pos):
    """
    Fuehrt die komplette Zeitleiste aus: alle Motoren gleichzeitig,
    jeder mit Vorsteuerung + P-Regler gegen seine Soll-Position.
    Gibt True zurueck, wenn bis zum Ende gefahren wurde, False bei
    Abbruch durch den rechten Button.
    """
    total_ms = times[-1]
    start_time = utime.ticks_ms()
    segment = 0
    finished_at = None

    while True:
        if hub.right_button.was_pressed():
            return False

        elapsed = utime.ticks_diff(utime.ticks_ms(), start_time)
        t = elapsed * SPEED_FACTOR  # Zeit innerhalb der Aufzeichnung
        in_recording = t < total_ms

        if in_recording:
            # passendes Segment [times[segment], times[segment+1]] suchen
            while segment < len(times) - 2 and t >= times[segment + 1]:
                segment += 1
            span = times[segment + 1] - times[segment]
            if span > 0:
                frac = (t - times[segment]) / span
            else:
                frac = 1.0
        else:
            segment = len(times) - 2
            span = times[segment + 1] - times[segment]
            frac = 1.0

        all_close = True
        for port_letter in ports:
            p0 = targets[port_letter][segment]
            p1 = targets[port_letter][segment + 1]
            target = start_pos[port_letter] + p0 + (p1 - p0) * frac
            actual = motors[port_letter].get_degrees_counted()
            error = target - actual

            # Vorsteuerung: aufgezeichnete Geschwindigkeit (Grad/s -> %)
            if in_recording and span > 0:
                feed_deg_s = (p1 - p0) * 1000.0 / span * SPEED_FACTOR
                feed = feed_deg_s / DEG_PER_SEC_AT_100 * 100.0
            else:
                feed = 0

            power = feed + KP * error
            power = clamp(power, MAX_SPEED.get(port_letter, DEFAULT_MAX_SPEED))

            if abs(error) > SETTLE_TOLERANCE_DEG:
                all_close = False

            motors[port_letter].start(int(power))

        # Nach dem letzten Frame: nachregeln, bis alles nah am Ziel ist
        # oder der Timeout greift (verhindert Endlosschleife bei
        # blockiertem Greifer).
        if not in_recording:
            if finished_at is None:
                finished_at = utime.ticks_ms()
            waited = utime.ticks_diff(utime.ticks_ms(), finished_at)
            if all_close or waited > SETTLE_TIMEOUT_MS:
                return True

        wait_for_seconds(CONTROL_INTERVAL_MS / 1000)


def replay(ports, frames):
    """
    Spielt die geparsten Frames auf den passenden Motoren ab.
    """
    if len(frames) == 0:
        print("Keine Frames in ENCODED_RECORDING gefunden.")
        return

    motors = {}
    for port_letter in ports:
        motor = Motor(port_letter)
        motor.set_stall_detection(False)
        motor.set_stop_action(STOP_ACTION.get(port_letter, DEFAULT_STOP_ACTION))
        motors[port_letter] = motor

    times, targets = build_timeline(ports, frames)
    print("Ports:", ports, "Frames:", len(frames), "Dauer (s):", times[-1] / 1000)

    hub.light_matrix.show_image("SQUARE")  # "bereit"
    print("Warte auf linken Button (Start)...")
    while not hub.left_button.was_pressed():
        wait_for_seconds(0.05)

    hub.light_matrix.show_image("HAPPY")  # "Wiedergabe laeuft"
    print("Wiedergabe gestartet.")

    # Startposition merken: alle Soll-Positionen sind relativ dazu
    start_pos = {}
    for port_letter in ports:
        start_pos[port_letter] = motors[port_letter].get_degrees_counted()

    # alte Klicks des rechten Buttons verwerfen
    hub.right_button.was_pressed()

    completed = run_motors_simultaneously(motors, ports, times, targets, start_pos)
    stop_all(motors)

    if completed:
        hub.light_matrix.show_image("YES")
        print("Wiedergabe beendet.")
        for port_letter in ports:
            goal = start_pos[port_letter] + targets[port_letter][-1]
            actual = motors[port_letter].get_degrees_counted()
            print("Port", port_letter, "Ziel:", goal, "Ist:", actual, "Abweichung:", goal - actual)
    else:
        hub.light_matrix.show_image("NO")
        print("Wiedergabe abgebrochen.")


def main():
    ports, base, frames = parse_recording(ENCODED_RECORDING)
    replay(ports, frames)


main()
