"""
====================================================================
 WIEDERGABE-PROGRAMM fuer LEGO Education SPIKE App (3.6.1)
====================================================================

Was macht dieses Programm?
---------------------------
Es liest die eigene, kompakte Kodierung (erzeugt vom
Aufzeichnungs-Programm "motor_recorder.py") aus einer Variable
ein und spielt die aufgezeichnete Motor-Bewegung auf dem Hub ab.

Format der Kodierung (zur Erinnerung):
    <PortA>,<PortB>,...|<Base>|<Frame1>|<Frame2>|...
    Jeder Frame: <zeit_delta_base_n>;<Port><delta_pos><Port><delta_pos>...

WICHTIGE ANNAHME (bitte pruefen!):
Wie beim Aufzeichnungs-Skript gehe ich davon aus, dass du im
"Python"-Modus der SPIKE-App bist (`from spike import ...`).

ZWEITE ANNAHME / DESIGN-ENTSCHEIDUNG (bitte pruefen!):
Du hast 3 Motoren, davon 2 als mechanisches Paar (z.B. Antrieb) und
1 einzelnen. Fuer beliebige, pro Motor UNTERSCHIEDLICHE Grad-Werte
(wie sie bei einer Aufzeichnung entstehen) ist LEGOs `MotorPair`-
Klasse (move_tank usw.) nicht zuverlaessig geeignet, da sie fuer
Fahr-/Lenkbewegungen gedacht ist, nicht fuer beliebige unabhaengige
Positions-Replays.

Deshalb nutze ich stattdessen `Motor.start(speed)` (nicht-
blockierend) fuer JEDEN Motor eines Frames gleichzeitig und
ueberwache per Schleife `get_degrees_counted()`, bis jeder Motor
sein Ziel erreicht hat, dann wird er einzeln per `stop()` gestoppt.
Das ergibt echte Gleichzeitigkeit fuer beliebig viele Motoren,
unabhaengig davon, welche Ports mechanisch gepaart sind - eine
separate MotorPair-Konfiguration ist dafuer nicht noetig.
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

# Mit welcher Geschwindigkeit (0-100) sollen die Motoren die
# aufgezeichneten Positions-Aenderungen abfahren?
REPLAY_SPEED = 50

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
            # in derselben Reihenfolge wie im Header. Falls deine
            # Aufzeichnung das nicht garantiert, sag Bescheid.
            deltas[port_letter] = 0
            continue

        remaining = remaining[1:]# Port-Buchstabe entfernen

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
    parts = encoded_text.split("|")
    header_ports = parts[0]
    header_base = int(parts[1])
    frame_texts = parts[2:]

    ports = header_ports.split(",")

    frames = []
    for frame_text in frame_texts:
        frames.append(parse_frame(frame_text, ports, header_base))

    return ports, header_base, frames


def run_motors_simultaneously(motors, deltas):
    """
    Startet ALLE Motoren mit einem Delta != 0 gleichzeitig (nicht-
    blockierend per start()) und stoppt jeden Motor einzeln, sobald
    er sein Ziel erreicht hat. So bewegen sich z.B. deine 2 gepaarten
    Motoren wirklich zeitgleich mit dem einzelnen 3. Motor, auch wenn
    ihre Deltas unterschiedlich gross sind.
    """
    # Start-Position + Zielwert + Richtung fuer jeden aktiven Motor merken
    active = {}
    for port_letter, motor in motors.items():
        delta = deltas[port_letter]
        if delta == 0:
            continue

        start_pos = motor.get_degrees_counted()
        direction = 1 if delta > 0 else -1
        # negative Geschwindigkeit = Gegenrichtung
        motor.start(REPLAY_SPEED * direction)
        active[port_letter] = (start_pos, delta, direction)

    # Warten, bis jeder gestartete Motor sein Ziel erreicht hat, dann
    # diesen Motor stoppen. Wir vergleichen bewusst mit abs(), nicht
    # richtungsabhaengig (>= bzw. <=): so ist die Bedingung "Ziel
    # erreicht?" unabhaengig von Vorwaerts-/Rueckwaerts-Richtung immer
    # gleich einfach "wie viele Grad habe ich mich vom Start entfernt?"
    # -- das macht den Vergleich robust gegenueber Motor-Traegheit,
    # leichtem Ueberschwingen oder Rauschen um den Zielwert herum.
    while active:
        for port_letter in list(active.keys()):
            start_pos, delta, direction = active[port_letter]
            moved = motors[port_letter].get_degrees_counted() - start_pos

            reached_target = abs(moved) >= abs(delta)
            if reached_target:
                motors[port_letter].stop()
                del active[port_letter]


def replay(ports, frames):
    """
    Spielt die geparsten Frames auf den passenden Motoren ab.
    """
    motors = {}
    for port_letter in ports:
        motors[port_letter] = Motor(port_letter)

    hub.light_matrix.show_image("HAPPY")# zeigt "Wiedergabe laeuft" an
    print("Wiedergabe gestartet. Anzahl Frames:", len(frames))

    for time_delta_ms, deltas in frames:
        frame_start = utime.ticks_ms()

        # alle Motoren dieses Frames gleichzeitig anfahren
        run_motors_simultaneously(motors, deltas)

        # verbleibende Zeit bis zum naechsten Frame abwarten, damit das
        # Timing der Original-Aufzeichnung moeglichst erhalten bleibt
        elapsed_ms = utime.ticks_diff(utime.ticks_ms(), frame_start)
        remaining_ms = time_delta_ms - elapsed_ms
        if remaining_ms > 0:
            wait_for_seconds(remaining_ms / 1000)

    hub.light_matrix.show_image("SQUARE")
    print("Wiedergabe beendet.")


def main():
    ports, base, frames = parse_recording(ENCODED_RECORDING)
    replay(ports, frames)


main()
