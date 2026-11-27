# chat transcripts

Binary transcript format used by the support chat:

    file   := MAGIC(4 bytes "CHT1") frame*
    frame  := type(1 byte) length(int32, big endian) body(length bytes)

Frame types are listed in `chat.wire.FrameType`. Text bodies are UTF-8.

Build and run the demo:

    javac -d out $(find src -name '*.java') && java -cp out chat.Main
