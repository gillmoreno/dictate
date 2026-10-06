// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "dictate-engine",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(url: "https://github.com/fermionresearch/phonon-coreml", from: "1.1.1"),
    ],
    targets: [
        .executableTarget(
            name: "dictate-engine",
            dependencies: [
                .product(name: "PhononCoreML", package: "phonon-coreml"),
            ]
        ),
    ]
)
