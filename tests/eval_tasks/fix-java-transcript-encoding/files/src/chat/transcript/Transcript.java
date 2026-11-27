package chat.transcript;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class Transcript {
    private final String title;
    private final List<Message> messages = new ArrayList<>();
    private final List<String> events = new ArrayList<>();

    public Transcript(String title) {
        this.title = title;
    }

    public String title() {
        return title;
    }

    public Transcript say(String author, String text) {
        messages.add(new Message(author, text));
        return this;
    }

    public Transcript event(String text) {
        events.add(text);
        return this;
    }

    public List<Message> messages() {
        return Collections.unmodifiableList(messages);
    }

    public List<String> events() {
        return Collections.unmodifiableList(events);
    }
}
