import Foundation
import AVFoundation
import CoreMedia

enum Failure: Error { case message(String) }

func contentIdentifier(_ value: String) -> AVMetadataItem {
    let item = AVMutableMetadataItem()
    item.identifier = .quickTimeMetadataContentIdentifier
    item.value = value as NSString
    item.dataType = "com.apple.metadata.datatype.UTF-8"
    return item
}

func stillTimeInput() throws -> (AVAssetWriterInput, AVAssetWriterInputMetadataAdaptor) {
    let specification: NSDictionary = [
        kCMMetadataFormatDescriptionMetadataSpecificationKey_Identifier as String:
            "mdta/com.apple.quicktime.still-image-time",
        kCMMetadataFormatDescriptionMetadataSpecificationKey_DataType as String:
            "com.apple.metadata.datatype.int8"
    ]
    var description: CMFormatDescription?
    let status = CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
        allocator: kCFAllocatorDefault,
        metadataType: kCMMetadataFormatType_Boxed,
        metadataSpecifications: [specification] as CFArray,
        formatDescriptionOut: &description
    )
    guard status == noErr, let description else {
        throw Failure.message("Cannot create still-image-time description: \(status)")
    }
    let input = AVAssetWriterInput(mediaType: .metadata, outputSettings: nil,
                                   sourceFormatHint: description)
    return (input, AVAssetWriterInputMetadataAdaptor(assetWriterInput: input))
}

func makeMovie(source: URL, destination: URL, identifier: String) throws {
    let asset = AVURLAsset(url: source)
    let video = asset.tracks(withMediaType: .video)
    let audio = asset.tracks(withMediaType: .audio)
    guard !video.isEmpty else { throw Failure.message("No video track") }
    try? FileManager.default.removeItem(at: destination)

    let reader = try AVAssetReader(asset: asset)
    let writer = try AVAssetWriter(outputURL: destination, fileType: .mov)
    writer.metadata = [contentIdentifier(identifier)]

    var pairs: [(AVAssetReaderTrackOutput, AVAssetWriterInput)] = []
    for track in video + audio {
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
        guard reader.canAdd(output) else { throw Failure.message("Reader cannot pass through \(track.mediaType)") }
        reader.add(output)
        let input = AVAssetWriterInput(mediaType: track.mediaType, outputSettings: nil)
        input.expectsMediaDataInRealTime = false
        if track.mediaType == .video { input.transform = track.preferredTransform }
        guard writer.canAdd(input) else { throw Failure.message("Writer cannot pass through \(track.mediaType)") }
        writer.add(input)
        pairs.append((output, input))
    }

    let (metadataInput, adaptor) = try stillTimeInput()
    guard writer.canAdd(metadataInput) else { throw Failure.message("Writer rejects timed metadata") }
    writer.add(metadataInput)

    guard writer.startWriting() else { throw writer.error ?? Failure.message("Writer failed to start") }
    guard reader.startReading() else { throw reader.error ?? Failure.message("Reader failed to start") }
    writer.startSession(atSourceTime: .zero)

    let moment = CMTimeMultiplyByFloat64(asset.duration, multiplier: 0.5)
    let item = AVMutableMetadataItem()
    item.identifier = AVMetadataIdentifier(rawValue: "mdta/com.apple.quicktime.still-image-time")
    item.value = NSNumber(value: Int8(0))
    item.dataType = "com.apple.metadata.datatype.int8"
    let frame = CMTime(value: 1, timescale: 30)
    let group = AVTimedMetadataGroup(items: [item], timeRange: CMTimeRange(start: moment, duration: frame))
    guard adaptor.append(group) else { throw Failure.message("Cannot write still-image-time") }
    metadataInput.markAsFinished()

    let dispatch = DispatchGroup()
    let state = NSLock()
    var copyFailure: Error?
    for (index, pair) in pairs.enumerated() {
        let (output, input) = pair
        dispatch.enter()
        input.requestMediaDataWhenReady(on: DispatchQueue(label: "live.copy.\(index)")) {
            while input.isReadyForMoreMediaData {
                guard let sample = output.copyNextSampleBuffer() else {
                    input.markAsFinished()
                    dispatch.leave()
                    return
                }
                if !input.append(sample) {
                    state.lock()
                    copyFailure = writer.error ?? Failure.message("Could not copy media sample")
                    state.unlock()
                    reader.cancelReading()
                    input.markAsFinished()
                    dispatch.leave()
                    return
                }
            }
        }
    }
    dispatch.wait()
    if let copyFailure { writer.cancelWriting(); throw copyFailure }
    guard reader.status == .completed else {
        writer.cancelWriting()
        throw reader.error ?? Failure.message("Reader incomplete")
    }
    let completed = DispatchSemaphore(value: 0)
    writer.finishWriting { completed.signal() }
    completed.wait()
    guard writer.status == .completed else {
        throw writer.error ?? Failure.message("Writer incomplete")
    }
    print("output", destination.path)
    print("asset ID", identifier)
    print("duration", CMTimeGetSeconds(asset.duration), "still frame", CMTimeGetSeconds(moment))
    print("video tracks", video.count, "audio tracks", audio.count)
}

guard CommandLine.arguments.count == 4 else {
    fatalError("usage: make_live_mov.swift input.mp4 output.mov ASSET-ID")
}
do {
    try makeMovie(source: URL(fileURLWithPath: CommandLine.arguments[1]),
                  destination: URL(fileURLWithPath: CommandLine.arguments[2]),
                  identifier: CommandLine.arguments[3])
} catch {
    fputs("Error: \(error)\n", stderr)
    exit(1)
}
