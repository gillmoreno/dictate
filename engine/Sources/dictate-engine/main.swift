// Resident Phonon-2 runner. The encoder stays on the Neural Engine and the
// decoder is the package's C loop. Python's reference runner decodes in
// numpy, which is the slow path, so dictation talks to this process instead.
//
// stdout is one JSON object per line. The first line is {"ready": true}.
// Each later stdin line is a wav path; the reply is
// {"text", "audio_s", "wall_s"} or {"error"}. "quit" exits.
import Foundation
import PhononCoreML

final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var shown = false

    func show(_ text: String) {
        lock.lock()
        defer { lock.unlock() }
        if shown { return }
        shown = true
        fputs(text, stderr)
        fflush(stderr)
    }
}

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object) else {
        fputs("dictate-engine: could not write a reply\n", stderr)
        exit(1)
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

@MainActor
func run() throws {
    let args = Array(CommandLine.arguments.dropFirst())
    guard args.count == 1 else {
        fputs("usage: dictate-engine <Phonon-2-CoreML folder>\n", stderr)
        exit(2)
    }
    let bundle = URL(fileURLWithPath: args[0])
    guard FileManager.default.fileExists(
        atPath: bundle.appendingPathComponent("manifest.json").path) else {
        fputs("dictate-engine: \(args[0]) has no manifest.json\n", stderr)
        exit(1)
    }

    var opt = Transcriber.Options()
    // Load the short function first so the message can appear, then wait
    // until every window size is compiled. A clip must not be the thing
    // that pays for a first-time Neural Engine compile.
    opt.eagerFunctions = [5]
    opt.backgroundLoad = true
    let once = Once()
    opt.progress = { phase, _ in
        // "loading" also fires when a compiled model is opened, which is
        // quick. The long wait is the one-time compile.
        if phase == "compiling" {
            once.show(
                "Preparing the Neural Engine for this Mac. "
                    + "The first launch can take several minutes. Later launches load straight away.\n")
        }
    }

    let started = Date()
    let transcriber = try Transcriber(bundle: bundle, options: opt)
    transcriber.waitForBackgroundLoads()
    // Silence is skipped. A quiet noise clip forces one real encode so the
    // first spoken take is not the one that warms the function.
    let noise = (0..<16_000).map { _ in Float.random(in: -0.02...0.02) }
    _ = try transcriber.transcribe(noise)
    let load = Date().timeIntervalSince(started)
    fputs(String(format: "Phonon-2 is on the Neural Engine (%.1f s).\n", load), stderr)
    fflush(stderr)
    emit(["ready": true])

    while let line = readLine() {
        let path = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if path.isEmpty { continue }
        if path == "quit" { return }
        do {
            let t0 = Date()
            let transcript = try transcriber.transcribe(url: URL(fileURLWithPath: path))
            emit([
                "text": transcript.text,
                "audio_s": transcript.audioSeconds,
                "wall_s": Date().timeIntervalSince(t0),
            ])
        } catch {
            emit(["error": error.localizedDescription])
        }
    }
}

do {
    try run()
} catch {
    fputs("dictate-engine: \(error.localizedDescription)\n", stderr)
    exit(1)
}
