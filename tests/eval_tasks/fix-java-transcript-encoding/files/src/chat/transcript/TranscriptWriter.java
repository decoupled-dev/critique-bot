package chat.transcript;

import chat.wire.Frame;
import chat.wire.FrameEncoder;
import chat.wire.FrameType;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;

public final class TranscriptWriter {
    static final byte[] MAGIC = {'C', 'H', 'T', '1'};

    private TranscriptWriter() {}

    public static void write(Transcript t, OutputStream out) throws IOException {
        out.write(MAGIC);
        FrameEncoder enc = new FrameEncoder(out);
        enc.write(new Frame(FrameType.HEADER, t.title()));
        for (String e : t.events()) {
            enc.write(new Frame(FrameType.SYSTEM, e));
        }
        for (Message m : t.messages()) {
            enc.write(new Frame(FrameType.MESSAGE, m.encode()));
        }
        enc.write(new Frame(FrameType.END, ""));
        enc.flush();
    }

    public static void save(Transcript t, Path path) throws IOException {
        try (OutputStream out = Files.newOutputStream(path)) {
            write(t, out);
        }
    }
}
