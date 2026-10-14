import AppKit
import Foundation

// Address only the fresh demo process by PID; never automate the user's
// existing Ghostty instance or whichever tab happens to have focus.
func code(_ value: String) -> OSType {
    precondition(value.utf8.count == 4)
    return value.utf8.reduce(0) { ($0 << 8) | OSType($1) }
}

func property(_ name: String, of container: NSAppleEventDescriptor = .null()) -> NSAppleEventDescriptor {
    let record = NSAppleEventDescriptor.record()
    record.setDescriptor(.init(typeCode: code("prop")), forKeyword: code("want"))
    record.setDescriptor(.init(enumCode: code("prop")), forKeyword: code("form"))
    record.setDescriptor(.init(typeCode: code(name)), forKeyword: code("seld"))
    record.setDescriptor(container, forKeyword: code("from"))
    return record.coerce(toDescriptorType: code("obj "))!
}

struct Ghostty {
    let pid: pid_t

    func send(_ eventClass: String = "Ghst", _ eventID: String,
              _ arguments: [String: NSAppleEventDescriptor] = [:]) throws -> NSAppleEventDescriptor {
        let event = NSAppleEventDescriptor(eventClass: code(eventClass), eventID: code(eventID),
            targetDescriptor: NSAppleEventDescriptor(processIdentifier: pid),
            returnID: -1, transactionID: 0)
        for (key, value) in arguments { event.setParam(value, forKeyword: code(key)) }
        let reply = try event.sendEvent(options: [.waitForReply], timeout: 10)
        if let error = reply.paramDescriptor(forKeyword: code("errn")), error.int32Value != 0 {
            let message = reply.paramDescriptor(forKeyword: code("errs"))?.stringValue ?? "Apple Event error"
            throw NSError(domain: "Blink.Ghostty", code: Int(error.int32Value),
                          userInfo: [NSLocalizedDescriptionKey: message])
        }
        return reply.paramDescriptor(forKeyword: code("----")) ?? .null()
    }

    func get(_ reference: NSAppleEventDescriptor) throws -> NSAppleEventDescriptor {
        try send("core", "getd", ["----": reference])
    }

    func split(_ terminal: NSAppleEventDescriptor, _ direction: String) throws -> NSAppleEventDescriptor {
        try send("Ghst", "Splt", ["----": terminal, "GSpd": .init(enumCode: code(direction))])
    }
}

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: GhosttyDemo /Applications/Ghostty.app /absolute/demo.config\n", stderr)
    exit(2)
}

let configuration = NSWorkspace.OpenConfiguration()
configuration.createsNewApplicationInstance = true
configuration.arguments = ["--config-default-files=false", "--config-file=" + CommandLine.arguments[2]]
var launched: NSRunningApplication?
var failure: Error?
var complete = false
NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: CommandLine.arguments[1]),
    configuration: configuration) { app, error in
        launched = app
        failure = error
        complete = true
    }
let deadline = Date().addingTimeInterval(15)
while !complete && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
guard let app = launched else {
    fputs("Cannot launch the demo: \(failure.map { String(describing: $0) } ?? "LaunchServices timed out")\n", stderr)
    exit(1)
}
print("Demo Ghostty PID: \(app.processIdentifier)")
let ghostty = Ghostty(pid: app.processIdentifier)
let runtime = URL(fileURLWithPath: CommandLine.arguments[2]).deletingLastPathComponent()

func waitForPane(_ number: Int) throws {
    let deadline = Date().addingTimeInterval(5)
    while Date() < deadline {
        if FileManager.default.fileExists(atPath: runtime.appendingPathComponent("\(number).json").path) {
            return
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    throw NSError(domain: "Blink.Ghostty", code: 2,
        userInfo: [NSLocalizedDescriptionKey: "Demo session \(number) did not register."])
}

do {
    // The first window is created asynchronously after NSWorkspace's callback.
    var first: NSAppleEventDescriptor?
    let readyDeadline = Date().addingTimeInterval(8)
    while Date() < readyDeadline {
        if let candidate = try? ghostty.get(property("GTfT", of: property("GWsT", of: property("GFWn")))),
           candidate.descriptorType == code("obj ") {
            first = candidate
            break
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
    }
    guard let topLeft = first else {
        throw NSError(domain: "Blink.Ghostty", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "The first terminal could not be addressed."])
    }
    try waitForPane(1)
    let topRight = try ghostty.split(topLeft, "GSrt")
    try waitForPane(2)
    _ = try ghostty.split(topRight, "GSdn")
    try waitForPane(3)
    let bottomLeft = try ghostty.split(topLeft, "GSdn")
    try waitForPane(4)
    _ = try ghostty.send("Ghst", "PfAc", ["----": .init(string: "equalize_splits"), "GonT": bottomLeft])
    _ = try ghostty.send("Ghst", "Fcus", ["----": bottomLeft])
    print("Four-panel demo opened. Only session 2 will request attention.")
} catch {
    // Keep the isolated demo usable if macOS automation is unavailable.
    fputs("Automatic layout unavailable: \(error.localizedDescription)\n", stderr)
    fputs("The demo window is open. Use Cmd+D and Cmd+Shift+D to add panels manually.\n", stderr)
    exit(3)
}
