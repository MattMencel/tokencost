// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TokenCostBar",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "TokenCostBar",
            path: "Sources/TokenCostBar"
        )
    ]
)
