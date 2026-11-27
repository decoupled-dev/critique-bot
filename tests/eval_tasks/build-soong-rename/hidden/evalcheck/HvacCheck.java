package evalcheck;

import com.example.hvac.common.FanController;
import com.example.hvac.ui.FanPanel;
import com.example.hvac.voice.VoiceCommandHandler;

/** Exercises the callers after the fix; exits 1 on the first wrong result. */
public class HvacCheck {
    static void expect(boolean ok, String what) {
        if (!ok) {
            System.out.println("wrong: " + what);
            System.exit(1);
        }
    }

    public static void main(String[] args) {
        FanController fan = new FanController(0);
        FanPanel panel = new FanPanel(fan);
        panel.onPlusClicked();
        panel.onPlusClicked();
        expect(fan.getFanLevel() == 2 && panel.shownLevel() == 2, "plus twice should give level 2");
        panel.onMinusClicked();
        expect(fan.getFanLevel() == 1 && panel.shownLevel() == 1, "minus should give level 1");
        for (int i = 0; i < 10; i++) panel.onPlusClicked();
        expect(panel.shownLevel() == FanController.MAX_LEVEL, "plus should stop at MAX_LEVEL");
        VoiceCommandHandler voice = new VoiceCommandHandler(fan);
        expect(voice.onSetFan(3).equals("Fan set to 3") && fan.getFanLevel() == 3, "voice set 3");
        expect(voice.onSetFan(12).equals("Fan set to the closest level, 7"), "voice set 12");
        System.out.println("ok");
    }
}
