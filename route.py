"""
====================================================================
 ROUTE AUFNEHMEN UND ABFAHREN -- alles in einem Programm
 fuer LEGO Education SPIKE App 3 (Python: hub, motor, time)
====================================================================

Warum ein einziges Programm?
-----------------------------
Die Konsole der App kann nur wenige hundert Zeichen am Stueck anzeigen.
Eine laengere Aufzeichnung laesst sich daraus nicht zuverlaessig
herauskopieren. Deshalb bleibt die Aufzeichnung hier im Speicher des Hubs
und wird direkt von demselben Programm abgefahren. Es muss nichts
kopiert werden.

Bedienung (nur die zwei Tasten links und rechts):
    1. Programm starten. Quadrat = bereit.
    2. LINKS druecken  -> Aufzeichnung startet (Smiley).
       Roboter und Greifer von Hand bewegen.
    3. RECHTS druecken -> Aufzeichnung endet. Haken = Aufzeichnung fertig.
    4. Roboter zurueck auf die Startposition stellen (Greifer in
       Grundstellung), dann
          LINKS  = Route abfahren (Herz), jederzeit mit RECHTS abbrechen
          RECHTS = alles verwerfen und neu aufzeichnen
    5. Nach dem Abfahren geht es wieder mit Schritt 4 weiter, die
       Aufzeichnung kann beliebig oft wiederholt werden.

Die Aufzeichnung nutzt weiterhin die eigene kompakte Kodierung
(Format: PORTS|BASE|frame|frame|...). Mit PRINT_RECORDING = True wird
sie zusaetzlich in kurzen Zeilen in der Konsole ausgegeben.

Wie funktioniert die Wiedergabe?
---------------------------------
Es werden KEINE Sensoren gebraucht, nur die Drehgeber der Motoren.
Aus der Aufzeichnung ergibt sich zu jedem Zeitpunkt eine Soll-Position
(zwischen den Frames linear interpoliert) und eine Soll-Geschwindigkeit.
Jeder Motor wird gleichzeitig geregelt: Vorsteuerung mit der
aufgezeichneten Geschwindigkeit plus P-Regler auf die Positions-
abweichung. So addieren sich Fehler nicht auf.
"""

from hub import port, light_matrix, button
import motor
import time


# ====================================================================
# KONFIGURATION -- hier alles einstellen, ohne den Rest anzufassen
# ====================================================================

# --- Aufzeichnung ---------------------------------------------------

# Welche Ports sollen aufgezeichnet werden? "A" bis "F".
# A, B = Fahrmotoren, E, F = Greifer / Werkzeug.
# Ports, an denen kein Motor haengt, werden automatisch uebersprungen.
PORTS_TO_RECORD = ["A", "B", "E", "F"]

# Messungen pro Sekunde. Hoeher = genauer, aber mehr Speicher.
SAMPLE_RATE_HZ = 10

# Kodierungs-Basis (max. 36).
ENCODING_BASE = 36

# Aufzeichnung zusaetzlich in der Konsole ausgeben (zum Aufheben)?
PRINT_RECORDING = False
OUTPUT_LINE_LENGTH = 60

# --- Wiedergabe -----------------------------------------------------

# Tempo: 1.0 = wie aufgezeichnet, 0.5 = halb so schnell (zum Testen).
SPEED_FACTOR = 1.0

# Reglerstaerke: zusaetzliche Geschwindigkeit (Grad/s) pro Grad Abweichung.
# Hinkt der Roboter hinterher -> erhoehen. Ruckelt/schwingt er -> senken.
KP = 8.0

# Maximale Geschwindigkeit pro Port in Grad pro Sekunde.
# Grenzen: Small ca. 660, Medium ca. 1110, Large ca. 1050.
# 600 ist fuer alle Typen sicher. Greifer (E, F) bewusst sanfter.
DEFAULT_MAX_VELOCITY = 600
MAX_VELOCITY = {"A": 600, "B": 600, "E": 300, "F": 300}

# Verhalten am Ende / bei Abbruch: motor.BRAKE, motor.HOLD oder motor.COAST.
DEFAULT_STOP_ACTION = motor.BRAKE
STOP_ACTION = {"A": motor.BRAKE, "B": motor.BRAKE, "E": motor.HOLD, "F": motor.HOLD}

# Regelintervall in Millisekunden
CONTROL_INTERVAL_MS = 20

# Nach dem letzten Frame so lange (max.) nachregeln, bis alle Motoren
# nah genug am Ziel sind. Danach wird in jedem Fall gestoppt.
SETTLE_TIMEOUT_MS = 1500
SETTLE_TOLERANCE_DEG = 3

# --- Ausgaben in der Konsole ----------------------------------------

# Alle wie viele Millisekunden soll waehrend der Aufzeichnung und der
# Fahrt eine Statuszeile ausgegeben werden? 0 = keine Statuszeilen.
LOG_INTERVAL_MS = 500


# ====================================================================
# AB HIER: Programmlogik (normalerweise nicht aendern)
# ====================================================================

PORT_IDS = {
    "A": port.A,
    "B": port.B,
    "C": port.C,
    "D": port.D,
    "E": port.E,
    "F": port.F,
}

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


# --------------------------------------------------------------------
# Hilfsfunktionen: Tasten und Motoren
# --------------------------------------------------------------------

def is_pressed(which_button):
    """True, solange der angegebene Button gedrueckt ist."""
    return button.pressed(which_button) > 0


def wait_release(which_button):
    """Wartet, bis der Button losgelassen wurde."""
    while is_pressed(which_button):
        time.sleep_ms(20)


def wait_for_choice():
    """Wartet auf LINKS oder RECHTS und gibt "left" oder "right" zurueck."""
    while True:
        if is_pressed(button.LEFT):
            wait_release(button.LEFT)
            return "left"
        if is_pressed(button.RIGHT):
            wait_release(button.RIGHT)
            return "right"
        time.sleep_ms(20)


def read_position(port_letter):
    """Aktuelle Motor-Position in Grad (Drehgeber)."""
    return motor.relative_position(PORT_IDS[port_letter])


def detect_ports():
    """
    Prueft, an welchen der konfigurierten Ports ein Motor erkannt wird.
    Fehlende Ports werden gemeldet und uebersprungen.
    """
    found = []
    for port_letter in PORTS_TO_RECORD:
        try:
            read_position(port_letter)
            found.append(port_letter)
        except OSError:
            print("Kein Motor an Port", port_letter, "erkannt - wird uebersprungen.")
    return found


def stop_all(ports):
    """Stoppt alle Motoren (mit der je Port eingestellten Stopp-Aktion)."""
    for port_letter in ports:
        motor.stop(
            PORT_IDS[port_letter],
            stop=STOP_ACTION.get(port_letter, DEFAULT_STOP_ACTION),
        )


def clamp(value, limit):
    """Begrenzt value auf den Bereich -limit .. +limit."""
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


# --------------------------------------------------------------------
# Kodierung (Aufzeichnung) und Dekodierung (Wiedergabe)
# --------------------------------------------------------------------

def encode_number(n):
    """Ganzzahl -> kompakte Base-N-Zeichenkette ("-" Praefix bei negativ)."""
    if n == 0:
        return "0"

    negative = n < 0
    n = abs(n)
    result = ""
    while n > 0:
        n, remainder = divmod(n, ENCODING_BASE)
        result = DIGITS[remainder] + result

    if negative:
        result = "-" + result
    return result


def decode_number(text, base):
    """Umkehrung von encode_number()."""
    if text == "":
        return 0

    negative = text.startswith("-")
    if negative:
        text = text[1:]

    value = 0
    for char in text:
        value = value * base + DIGITS.index(char)

    return -value if negative else value


def encode_frame(timestamp_ms, positions, previous, active_ports):
    """
    Kodiert EINEN Zeitpunkt: <zeit_delta_ms>;<Port><delta_grad>...
    Beispiel: "a;A5B-2" -> 10 ms seit dem letzten Frame, A +5, B -2 Grad.
    """
    time_delta = timestamp_ms - previous["_time"]

    port_codes = ""
    for port_letter in active_ports:
        delta = positions[port_letter] - previous[port_letter]
        port_codes += port_letter + encode_number(delta)

    return encode_number(time_delta) + ";" + port_codes


def parse_frame(frame_text, ports, base):
    """Frame-String -> (zeit_delta_ms, {port: delta_position})."""
    time_part, ports_part = frame_text.split(";")
    time_delta = decode_number(time_part, base)

    deltas = {}
    remaining = ports_part
    for port_letter in ports:
        if not remaining.startswith(port_letter):
            deltas[port_letter] = 0
            continue

        remaining = remaining[1:]  # Port-Buchstabe entfernen

        number_text = ""
        while remaining and remaining[0] not in ports:
            number_text += remaining[0]
            remaining = remaining[1:]

        deltas[port_letter] = decode_number(number_text, base)

    return time_delta, deltas


def parse_recording(encoded_text):
    """Komplette Kodierung -> (ports, base, frames)."""
    parts = encoded_text.split("|")
    if len(parts) < 3:
        raise ValueError("Aufzeichnung zu kurz")

    ports = parts[0].split(",")
    base = int(parts[1])

    frames = []
    for frame_text in parts[2:]:
        if frame_text == "" or ";" not in frame_text:
            continue
        frames.append(parse_frame(frame_text, ports, base))

    return ports, base, frames


def build_timeline(ports, frames):
    """
    Frame-Deltas -> kumulierte Zeitleiste:
        times   - Zeitpunkte in ms (beginnt bei 0)
        targets - Port -> kumulierte Positionen in Grad relativ zum Start
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


def print_recording(output):
    """Gibt die Aufzeichnung als kurze Zeilen im Python-Format aus."""
    print("ENCODED_RECORDING = (")
    start = 0
    while start < len(output):
        print('    "' + output[start:start + OUTPUT_LINE_LENGTH] + '"')
        time.sleep_ms(30)  # kleine Pause, damit keine Zeilen verloren gehen
        start += OUTPUT_LINE_LENGTH
    print(")")


# --------------------------------------------------------------------
# Aufzeichnen
# --------------------------------------------------------------------

def record(active_ports):
    """
    Zeichnet Motor-Positionen auf, bis RECHTS gedrueckt wird.
    Gibt die Aufzeichnung als Text im Format PORTS|BASE|frame|... zurueck.
    """
    light_matrix.show_image(light_matrix.IMAGE_HAPPY)  # Aufzeichnung laeuft
    print("Aufzeichnung laeuft. RECHTS = Stopp.")

    encoded_frames = []
    start_time = time.ticks_ms()

    previous = {"_time": 0}
    for port_letter in active_ports:
        previous[port_letter] = read_position(port_letter)

    sample_interval_ms = int(1000 / SAMPLE_RATE_HZ)
    last_log = 0

    start_text = ""
    for port_letter in active_ports:
        start_text += port_letter + "=" + str(previous[port_letter]) + " "
    print("Startpositionen (Grad):", start_text)

    while not is_pressed(button.RIGHT):
        now = time.ticks_diff(time.ticks_ms(), start_time)

        current_positions = {}
        for port_letter in active_ports:
            current_positions[port_letter] = read_position(port_letter)

        encoded_frames.append(encode_frame(now, current_positions, previous, active_ports))

        # Statuszeile in festem Abstand
        if LOG_INTERVAL_MS > 0 and now - last_log >= LOG_INTERVAL_MS:
            last_log = now
            log_line = ""
            for port_letter in active_ports:
                log_line += port_letter + "=" + str(current_positions[port_letter]) + " "
            print("Aufzeichnung t=" + str(now / 1000) + " s, Frames: " + str(len(encoded_frames)) + ", Positionen: " + log_line)

        previous["_time"] = now
        for port_letter in active_ports:
            previous[port_letter] = current_positions[port_letter]

        # Schleifendauer abziehen, damit die Abtastrate stabil bleibt
        spent = time.ticks_diff(time.ticks_ms(), start_time) - now
        wait_ms = sample_interval_ms - spent
        if wait_ms < 1:
            wait_ms = 1
        time.sleep_ms(wait_ms)

    wait_release(button.RIGHT)
    total_ms = time.ticks_diff(time.ticks_ms(), start_time)
    print("Stopp gedrueckt nach", total_ms / 1000, "s.")

    header = ",".join(active_ports) + "|" + str(ENCODING_BASE)
    return header + "|" + "|".join(encoded_frames)


# --------------------------------------------------------------------
# Abfahren
# --------------------------------------------------------------------

def run_motors_simultaneously(ports, times, targets, start_pos, max_error):
    """
    Fuehrt die Zeitleiste aus: alle Motoren gleichzeitig, jeder mit
    Vorsteuerung + P-Regler gegen seine Soll-Position.
    Gibt True zurueck, wenn bis zum Ende gefahren wurde, False bei
    Abbruch durch RECHTS.
    """
    total_ms = times[-1]
    start_time = time.ticks_ms()
    segment = 0
    finished_at = None
    last_log = 0

    while True:
        if is_pressed(button.RIGHT):
            return False

        elapsed = time.ticks_diff(time.ticks_ms(), start_time)
        t = elapsed * SPEED_FACTOR  # Zeit innerhalb der Aufzeichnung
        in_recording = t < total_ms

        if in_recording:
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
        log_line = ""
        for port_letter in ports:
            p0 = targets[port_letter][segment]
            p1 = targets[port_letter][segment + 1]
            target = start_pos[port_letter] + p0 + (p1 - p0) * frac
            actual = read_position(port_letter)
            error = target - actual

            if abs(error) > max_error[port_letter]:
                max_error[port_letter] = int(abs(error))

            # Vorsteuerung: aufgezeichnete Geschwindigkeit in Grad/s
            if in_recording and span > 0:
                feed = (p1 - p0) * 1000.0 / span * SPEED_FACTOR
            else:
                feed = 0

            velocity = feed + KP * error
            velocity = clamp(velocity, MAX_VELOCITY.get(port_letter, DEFAULT_MAX_VELOCITY))

            if abs(error) > SETTLE_TOLERANCE_DEG:
                all_close = False

            motor.run(PORT_IDS[port_letter], int(velocity))

            log_line += port_letter + ": Soll " + str(int(target)) + " Ist " + str(actual) + " v=" + str(int(velocity)) + "   "

        # Statuszeile in festem Abstand
        if LOG_INTERVAL_MS > 0 and elapsed - last_log >= LOG_INTERVAL_MS:
            last_log = elapsed
            print("Fahrt t=" + str(elapsed / 1000) + " s von " + str(total_ms / SPEED_FACTOR / 1000) + " s | " + log_line)

        # Nach dem letzten Frame: nachregeln, bis alles nah am Ziel ist
        # oder der Timeout greift (verhindert Endlosschleife bei
        # blockiertem Greifer).
        if not in_recording:
            if finished_at is None:
                finished_at = time.ticks_ms()
            waited = time.ticks_diff(time.ticks_ms(), finished_at)
            if all_close or waited > SETTLE_TIMEOUT_MS:
                return True

        time.sleep_ms(CONTROL_INTERVAL_MS)


def replay(ports, times, targets):
    """Faehrt die Aufzeichnung einmal ab."""
    light_matrix.show_image(light_matrix.IMAGE_HEART)  # Wiedergabe laeuft
    print("Wiedergabe laeuft. RECHTS = Abbruch.")

    # Startposition merken: alle Soll-Positionen sind relativ dazu
    start_pos = {}
    for port_letter in ports:
        start_pos[port_letter] = read_position(port_letter)

    max_error = {}
    for port_letter in ports:
        max_error[port_letter] = 0

    start_text = ""
    for port_letter in ports:
        start_text += port_letter + "=" + str(start_pos[port_letter]) + " "
    print("Startpositionen (Grad):", start_text)

    run_start = time.ticks_ms()
    completed = False
    try:
        completed = run_motors_simultaneously(ports, times, targets, start_pos, max_error)
    finally:
        # Motoren werden in jedem Fall gestoppt, auch bei einem Fehler
        stop_all(ports)

    if completed:
        print("Wiedergabe beendet nach", time.ticks_diff(time.ticks_ms(), run_start) / 1000, "s.")
        for port_letter in ports:
            goal = start_pos[port_letter] + targets[port_letter][-1]
            actual = read_position(port_letter)
            print("Port", port_letter, "Ziel:", goal, "Ist:", actual, "Abweichung am Ende:", goal - actual, "Max. Fehler waehrend der Fahrt:", max_error[port_letter])
    else:
        print("Wiedergabe abgebrochen nach", time.ticks_diff(time.ticks_ms(), run_start) / 1000, "s.")
        wait_release(button.RIGHT)


# --------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------

def main():
    active_ports = detect_ports()
    if len(active_ports) == 0:
        light_matrix.show_image(light_matrix.IMAGE_NO)
        print("Kein Motor an den Ports", PORTS_TO_RECORD, "gefunden. Bitte Kabel pruefen.")
        return

    print("Ports:", active_ports)
    print("Einstellungen: Abtastrate", SAMPLE_RATE_HZ, "Hz, Tempo", SPEED_FACTOR, ", KP", KP, ", Status alle", LOG_INTERVAL_MS, "ms")

    ports = None
    times = None
    targets = None

    while True:
        if times is None:
            # --- noch keine Aufzeichnung: auf LINKS warten ---
            light_matrix.show_image(light_matrix.IMAGE_SQUARE)  # bereit
            print("Bereit. LINKS = Aufzeichnung starten.")
            if wait_for_choice() != "left":
                continue

            recording_text = record(active_ports)
            ports, base, frames = parse_recording(recording_text)

            if len(frames) < 2:
                print("Aufzeichnung zu kurz, bitte erneut versuchen.")
                times = None
                continue

            times, targets = build_timeline(ports, frames)
            print("Aufzeichnung fertig. Frames:", len(frames), "Dauer (s):", times[-1] / 1000)
            if times[-1] > 0:
                print("Echte Abtastrate:", len(frames) * 1000 / times[-1], "Hz")
            for port_letter in ports:
                values = targets[port_letter]
                path = 0
                for i in range(1, len(values)):
                    path += abs(values[i] - values[i - 1])
                print("Port", port_letter, ": Ende", values[-1], "Grad, kleinster Wert", min(values), ", groesster Wert", max(values), ", Weg gesamt", path, "Grad")

            if PRINT_RECORDING:
                print("=== AUFZEICHNUNG ===")
                print_recording(recording_text)
                print("=== ENDE ===")
        else:
            # --- Aufzeichnung vorhanden: abfahren oder neu aufzeichnen ---
            light_matrix.show_image(light_matrix.IMAGE_YES)  # Aufzeichnung fertig
            print("Roboter auf Startposition stellen. LINKS = abfahren, RECHTS = neu aufzeichnen.")
            if wait_for_choice() == "right":
                times = None
                targets = None
                continue

            replay(ports, times, targets)


main()
